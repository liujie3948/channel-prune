import os
import torch
from torch.autograd import Variable
import torchvision.models as models
import cv2
import numpy as np
import torchvision
from torch import nn
import torch.nn.functional as F
import torch.optim as optim
import dataset
import argparse
from operator import itemgetter
from heapq import nsmallest
import time
from models.inception_res_v1 import BasicConv2d, Block35, Block17, Block8, \
                         Mixed_6a, Mixed_7a , InceptionResnetV1
# from models.inception_res_v2 import BasicConv2d, Block35, Block17, Block8, \
#                          Mixed_6a, Mixed_7a , InceptionResnetV2
from utils.train import train
from torchvision import transforms
import torch.utils.data as torchdata
import logging
from utils.train import train,trainlog
from torch.optim import lr_scheduler

os.environ["CUDA_VISIBLE_DEVICES"] = "0"


class FilterPrunner:
    def __init__(self, model,useCuda=True):
        self.model = model
        self.reset()
        self.useCuda = useCuda

    def reset(self):
        self.filter_ranks = {}
        self.index_to_layername = {}
        self.layername_to_index = {}
        self.hooks = []

    def remove_hooks(self):
        for h in self.hooks:
            h.remove()

    def hook_generator(self, layername):
        def hook_func(grad):
            activation_index = self.layername_to_index[layername]
            # print layername,'hook'
            activation = self.activations[activation_index]
            # print layername,grad.size(),activation_index,self.activations[activation_index].size()
            values = \
                torch.sum((activation * grad), dim=0, keepdim=True). \
                    sum(dim=2, keepdim=True).sum(dim=3, keepdim=True)[0, :, 0, 0].data
            # Normalize the rank by the filter dimensions
            values = \
                values / (activation.size(0) * activation.size(2) * activation.size(3))

            if activation_index not in self.filter_ranks:
                if self.useCuda:
                    self.filter_ranks[activation_index] = \
                        torch.FloatTensor(activation.size(1)).zero_().cuda()
                else:
                    self.filter_ranks[activation_index] = \
                        torch.FloatTensor(activation.size(1)).zero_()

            self.filter_ranks[activation_index] += values

        return hook_func

    def lowest_ranking_filters(self, num):
        data = []
        for i in sorted(self.filter_ranks.keys()):
            for j in range(self.filter_ranks[i].size(0)):
                data.append((i, j, self.filter_ranks[i][j]))

        return nsmallest(num, data, itemgetter(2))

    def normalize_ranks_per_layer(self):
        for i in self.filter_ranks:
            v = torch.abs(self.filter_ranks[i])
            v = v / np.sqrt(torch.sum(v * v))
            self.filter_ranks[i] = v.cpu()

    def get_prunning_plan(self, num_filters_to_prune):
        filters_to_prune = self.lowest_ranking_filters(num_filters_to_prune)  # [(actv_idx, channel_idx, value), ...]

        filters_to_prune_per_layer = {}
        for (l, f, _) in filters_to_prune:
            if l not in filters_to_prune_per_layer:
                filters_to_prune_per_layer[l] = []
            filters_to_prune_per_layer[l].append(f)

        return filters_to_prune_per_layer

    def forward(self, x):
        self.activations = []
        activation_index = 0

        # first 5 conv layers
        for i, (name1, module1) in enumerate(self.model._modules.items()):
            if i >= 7: break
            # print name1,'=='*20
            if name1 == 'maxpool_3a' or name1 == 'maxpool_5a':
                x = module1(x)
            for j, (name2, module2) in enumerate(module1._modules.items()):
                # print name1,name2, '--' * 20

                x = module2(x)
                if isinstance(module2, torch.nn.modules.conv.Conv2d):
                    hook = self.hook_generator((name1, name2))
                    h = x.register_hook(hook)
                    self.hooks.append(h)
                    self.activations.append(x)
                    # binding layer name and index
                    if not activation_index in self.index_to_layername.keys():
                        self.index_to_layername[activation_index] = (name1, name2)
                        self.layername_to_index[(name1, name2)] = activation_index
                    activation_index += 1

        for i, (name1, module1) in enumerate(self.model._modules.items()):
            # print name1, '==' * 20
            if i < 7: continue
            # Mixed_6a block
            elif isinstance(module1, Mixed_6a):
                branchs = {}
                branchs['branch0'] = x
                branchs['branch1'] = x
                branchs['branch2'] = x
                for j, (name2, module2) in enumerate(module1._modules.items()):
                    if name2.startswith('branch0'):
                        branch = 'branch0'
                    elif name2.startswith('branch1'):
                        branch = 'branch1'
                    elif name2.startswith('branch2'):
                        branch = 'branch2'
                    else:
                        raise ValueError('"%s" not in Mixed_6a block' % name2)
                    if isinstance(module2, nn.MaxPool2d):
                        branchs[branch] = module2(branchs[branch])
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        branchs[branch] = module3(branchs[branch])
                        # print name1, name2, name3, branch, branchs[branch].size()
                        if isinstance(module3, torch.nn.modules.conv.Conv2d):
                            hook = self.hook_generator((name1, name2, name3))
                            h = branchs[branch].register_hook(hook)
                            self.hooks.append(h)
                            self.activations.append(branchs[branch])
                            # binding layer name and index
                            if not activation_index in self.index_to_layername.keys():
                                self.index_to_layername[activation_index] = (name1, name2, name3)
                                self.layername_to_index[(name1, name2, name3)] = activation_index
                            activation_index += 1
                # concat
                x = torch.cat([branchs['branch0'], branchs['branch1'], branchs['branch2']], 1)

            # Mixed_7a
            elif isinstance(module1, Mixed_7a):
                branchs = {}
                branchs['branch0'] = x
                branchs['branch1'] = x
                branchs['branch2'] = x
                branchs['branch3'] = x
                for j, (name2, module2) in enumerate(module1._modules.items()):
                    if name2.startswith('branch0'):
                        branch = 'branch0'
                    elif name2.startswith('branch1'):
                        branch = 'branch1'
                    elif name2.startswith('branch2'):
                        branch = 'branch2'
                    elif name2.startswith('branch3'):
                        branch = 'branch3'
                    else:
                        raise ValueError('"%s" not in Mixed_7a block' % name2)
                    if isinstance(module2, nn.MaxPool2d):
                        branchs[branch] = module2(branchs[branch])
                        # print name1, name2,branch, branchs[branch].size()
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        branchs[branch] = module3(branchs[branch])
                        if isinstance(module3, torch.nn.modules.conv.Conv2d):
                            hook = self.hook_generator((name1, name2, name3))
                            h = branchs[branch].register_hook(hook)
                            self.hooks.append(h)
                            self.activations.append(branchs[branch])
                            # binding layer name and index
                            if not activation_index in self.index_to_layername.keys():
                                self.index_to_layername[activation_index] = (name1, name2, name3)
                                self.layername_to_index[(name1, name2, name3)] = activation_index
                            activation_index += 1
                # concat
                x = torch.cat([branchs['branch0'], branchs['branch1'], branchs['branch2'],branchs['branch3']], 1)

            # Squential repeat Block35
            elif isinstance(module1, nn.Sequential) and (name1=='repeat_block35'):
                # each Block35
                for j, (name2, module2) in enumerate(module1._modules.items()):
                    branchs = {}
                    branchs['branch0'] = x
                    branchs['branch1'] = x
                    branchs['branch2'] = x
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        # skip now
                        if isinstance(module3, nn.Conv2d) or isinstance(module3, nn.ReLU):
                           continue
                        if name3.startswith('branch0'):
                            branch = 'branch0'
                        elif name3.startswith('branch1'):
                            branch = 'branch1'
                        elif name3.startswith('branch2'):
                            branch = 'branch2'
                        else:
                            raise ValueError('"%s" not in Block35 block' % name3)
                        for l, (name4, module4) in enumerate(module3._modules.items()):
                            branchs[branch] = module4(branchs[branch])
                            if isinstance(module4, torch.nn.modules.conv.Conv2d):
                                hook = self.hook_generator((name1, name2, name3, name4))
                                h = branchs[branch].register_hook(hook)
                                self.hooks.append(h)
                                self.activations.append(branchs[branch])
                                # binding layer name and index
                                if not activation_index in self.index_to_layername.keys():
                                    self.index_to_layername[activation_index] = (name1, name2, name3, name4)
                                    self.layername_to_index[(name1, name2, name3, name4)] = activation_index
                                activation_index += 1
                    # after all branchs
                    out = torch.cat([branchs['branch0'], branchs['branch1'], branchs['branch2']],dim=1)
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        if isinstance(module3, nn.Conv2d):
                            out = module3(out)
                            x = x + module2.scale * out
                        elif isinstance(module3, nn.ReLU):
                            x = module3(x)

            # Squential repeat Block17
            elif isinstance(module1, nn.Sequential) and (name1=='repeat_block17'):
                # each Block17
                for j, (name2, module2) in enumerate(module1._modules.items()):
                    branchs = {}
                    branchs['branch0'] = x
                    branchs['branch1'] = x
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        # skip now
                        if isinstance(module3, nn.Conv2d) or isinstance(module3, nn.ReLU):
                           continue
                        if name3.startswith('branch0'):
                            branch = 'branch0'
                        elif name3.startswith('branch1'):
                            branch = 'branch1'
                        else:
                            raise ValueError('"%s" not in Block17 block' % name3)
                        for l, (name4, module4) in enumerate(module3._modules.items()):
                            branchs[branch] = module4(branchs[branch])
                            if isinstance(module4, torch.nn.modules.conv.Conv2d):
                                hook = self.hook_generator((name1, name2, name3, name4))
                                h = branchs[branch].register_hook(hook)
                                self.hooks.append(h)
                                self.activations.append(branchs[branch])
                                # binding layer name and index
                                if not activation_index in self.index_to_layername.keys():
                                    self.index_to_layername[activation_index] = (name1, name2, name3, name4)
                                    self.layername_to_index[(name1, name2, name3, name4)] = activation_index
                                activation_index += 1
                    # after all branchs
                    out = torch.cat([branchs['branch0'], branchs['branch1']],dim=1)
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        if isinstance(module3, nn.Conv2d):
                            out = module3(out)
                            x = x + module2.scale * out
                        elif isinstance(module3, nn.ReLU):
                            x = module3(x)

            # Squential repeat Block8
            elif isinstance(module1, nn.Sequential) and (name1=='repeat_block8'):
                # each Block17
                for j, (name2, module2) in enumerate(module1._modules.items()):
                    branchs = {}
                    branchs['branch0'] = x
                    branchs['branch1'] = x
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        # skip now
                        if isinstance(module3, nn.Conv2d) or isinstance(module3, nn.ReLU):
                           continue
                        if name3.startswith('branch0'):
                            branch = 'branch0'
                        elif name3.startswith('branch1'):
                            branch = 'branch1'
                        else:
                            raise ValueError('"%s" not in Block8 block' % name3)
                        for l, (name4, module4) in enumerate(module3._modules.items()):
                            branchs[branch] = module4(branchs[branch])
                            if isinstance(module4, torch.nn.modules.conv.Conv2d):
                                hook = self.hook_generator((name1, name2, name3, name4))
                                h = branchs[branch].register_hook(hook)
                                self.hooks.append(h)
                                self.activations.append(branchs[branch])
                                # binding layer name and index
                                if not activation_index in self.index_to_layername.keys():
                                    self.index_to_layername[activation_index] = (name1, name2, name3, name4)
                                    self.layername_to_index[(name1, name2, name3, name4)] = activation_index
                                activation_index += 1
                    # after all branchs
                    out = torch.cat([branchs['branch0'], branchs['branch1']],dim=1)
                    for k, (name3, module3) in enumerate(module2._modules.items()):
                        if isinstance(module3, nn.Conv2d):
                            out = module3(out)
                            x = x + module2.scale * out
                        elif isinstance(module3, nn.ReLU):
                            x = module3(x)

            # a Blcok8 (no Relu)
            elif isinstance(module1, Block8):
                branchs = {}
                branchs['branch0'] = x
                branchs['branch1'] = x
                for k, (name2, module2) in enumerate(module1._modules.items()):
                    # skip now
                    if isinstance(module2, nn.Conv2d) or isinstance(module2, nn.ReLU):
                        continue
                    if name2.startswith('branch0'):
                        branch = 'branch0'
                    elif name2.startswith('branch1'):
                        branch = 'branch1'
                    else:
                        raise ValueError('"%s" not in Block8 block' % name2)
                    for l, (name3, module3) in enumerate(module2._modules.items()):
                        branchs[branch] = module3(branchs[branch])
                        if isinstance(module3, torch.nn.modules.conv.Conv2d):
                            hook = self.hook_generator((name1, name2, name3))
                            h = branchs[branch].register_hook(hook)
                            self.hooks.append(h)
                            self.activations.append(branchs[branch])
                            # binding layer name and index
                            if not activation_index in self.index_to_layername.keys():
                                self.index_to_layername[activation_index] = (name1, name2, name3)
                                self.layername_to_index[(name1, name2, name3)] = activation_index
                            activation_index += 1
                # after all branchs
                out = torch.cat([branchs['branch0'], branchs['branch1']],dim=1)
                for k, (name2, module2) in enumerate(module1._modules.items()):
                    if isinstance(module2, nn.Conv2d):
                        out = module2(out)
                        x = x + module1.scale * out
                        # no Relu

            # avpool layer
            elif name1 in ['avgpool_1a', 'dropout', 'fc']:
                # print name1, module1
                x = module1(x)
                if name1 == 'avgpool_1a':
                    x = x.view(x.size(0), -1)

        return x

class PrunningFineTuner_IcepResV1:
    def __init__(self, train_path, test_path, model):
        self.train_data_loader = dataset.loader(train_path, batch_size=48)
        self.test_data_loader = dataset.test_loader(test_path, batch_size=24)
        self.model = model

        self.criterion = torch.nn.CrossEntropyLoss()
        self.prunner = FilterPrunner(self.model,useCuda=True)
        self.model.train()

    def test(self):
        self.model.eval()
        correct = 0
        total = 0
        for i, (inputs, labels) in enumerate(self.test_data_loader):
            inputs = Variable(inputs.cuda())
            labels = Variable(labels.cuda())
            output = self.model(inputs)
            pred = output.data.max(1)[1]
            correct += pred.eq(labels.data).sum()
            total += labels.size(0)

        print "Accuracy :", float(correct) / total

        self.model.train()

    def train(self, optimizer=None, epoches=10):
        if optimizer is None:
            optimizer = \
                optim.SGD(model.fc.parameters(),
                          lr=0.0001, momentum=0.9)

        self.test()
        for i in range(epoches):
            print "Epoch: ", i

            self.train_epoch(optimizer)
            self.test()
        print "Finished fine tuning."

    def train_batch(self, optimizer, batch, label, rank_filters):
        self.model.zero_grad()
        input = Variable(batch)
        label = Variable(label)
        if rank_filters:
            self.model.eval()
            output2 = self.prunner.forward(input)  # compute filter activation, register hook function
            self.criterion(output2, label).backward()  # compute filter grad by hook, and grad*activation

        else:
            self.model.train()
            loss = self.criterion(self.model(input), label)
            loss.backward()
            optimizer.step()


    def train_epoch(self, optimizer=None, rank_filters=False):
        if rank_filters:
            self.model.eval()
        else:
            self.model.train()
        for input, label in self.train_data_loader:
            self.model.zero_grad()
            input = Variable(input.cuda())
            label = Variable(label.cuda())
            if rank_filters:
                output = self.prunner.forward(input)  # compute filter activation, register hook function
                self.criterion(output, label).backward()  # compute filter grad by hook, and grad*activation

            else:
                loss = self.criterion(self.model(input), label)
                # print loss,optimizer
                loss.backward()
                optimizer.step()

    def get_candidates_to_prune(self, num_filters_to_prune):
        self.prunner.reset()

        self.train_epoch(rank_filters=True)
        self.prunner.remove_hooks()
        self.prunner.normalize_ranks_per_layer()
        return self.prunner.get_prunning_plan(num_filters_to_prune)

    def total_num_filters(self):
        filters = 0
        for name1, module1 in self.model._modules.items():
            if isinstance(module1, torch.nn.modules.conv.Conv2d):
                filters = filters + module1.out_channels
            for name2, module2 in module1._modules.items():
                if isinstance(module2, torch.nn.modules.conv.Conv2d):
                    filters = filters + module2.out_channels
                for name3, module3 in module2._modules.items():
                    if isinstance(module3, torch.nn.modules.conv.Conv2d):
                        filters = filters + module3.out_channels
        return filters

    def prune(self):
        # Get the accuracy before prunning
        self.test()

        self.model.eval()
        # Make sure all the layers are trainable
        for param in self.model.parameters():
            param.requires_grad = True

        number_of_filters = self.total_num_filters()
        num_filters_to_prune_per_iteration = 512
        iterations = int(float(number_of_filters) / num_filters_to_prune_per_iteration)

        iterations = int(iterations * 2.0 / 3)

        print "Number of prunning iterations to reduce 67% filters", iterations

        for _ in range(iterations):
            self.model.eval()
            prune_targets = self.get_candidates_to_prune(num_filters_to_prune_per_iteration)
            prune_targets_by_layername = {self.prunner.index_to_layername[idx]:prune_targets[idx] for idx in prune_targets.keys()}

            for layer_name in prune_targets_by_layername.keys():
                print layer_name, ':' ,prune_targets_by_layername[layer_name], ','

            layers_prunned = {self.prunner.index_to_layername[idx]: len(prune_targets[idx]) for idx in prune_targets.keys()}
            #
            print "Layers that will be prunned", layers_prunned
            print "Prunning filters.. "
            self.model.eval()

            # model = self.model.cpu()
            # model = prune_incep3_conv_layer(model, prune_targets_by_layername)
            # torch.save(model, "model_pruned.pth")
            # model = torch.load('model_pruned.pth').cuda()
            # self.model = model

            message = str(100 * float(self.total_num_filters()) / number_of_filters) + "%"
            print "Filters remain", str(message)
            self.test()
            print "Fine tuning to recover from prunning iteration."
            optimizer = optim.SGD(self.model.parameters(), lr=0.0001, momentum=0.9)
            self.train(optimizer, epoches=5)

            self.prunner.model = self.model  # refresh prunner's model or it will raise error "Tensors on different GPUs"


        print "Finished. Going to fine tune the model a bit more"
        self.train(optimizer, epoches=10)
        torch.save(model, "model_prunned.pth")


if __name__ == '__main__':
    isTrain = False
    isPrune = True
    train_path = '/home/gserver/zhangchi/channel-prune/data/train1'
    test_path = '/media/gserver/data/catavsdog/test1'

    if isTrain:
        model = InceptionResnetV1(2)
        # model = models.resnet50()
        # num_ftrs = model.fc.in_features
        # model.fc = nn.Linear(num_ftrs, 2)
        # freeze param except fc linear
        # for i, m in enumerate(list(model.children())[0:-1]):
        #     for p in m.parameters():
        #         p.requires_grad = True
        model = torch.nn.DataParallel(model)
        model = model.cuda()
    #
    elif isPrune:
        model = torch.load('./IncepResV1/model_finetune.pth').module.cuda()


    fine_tuner = PrunningFineTuner_IcepResV1(train_path, test_path, model)

    if isTrain:
        fine_tuner.train(epoches=10,optimizer = optim.SGD(model.parameters(),
                          lr=0.01, momentum=0.9))

        # usecuda = 1
        # start_epoch = 0
        # epoch_num = 50
        # save_inter = 10
        # batch_size = 48
        #
        # data_transforms = {
        #     'train': transforms.Compose([
        #         transforms.RandomSizedCrop(299),
        #         transforms.RandomHorizontalFlip(),
        #         transforms.ToTensor(),
        #         transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        #     ]),
        #     'val': transforms.Compose([
        #         transforms.Scale(314),
        #         transforms.CenterCrop(299),
        #         transforms.ToTensor(),
        #         transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        #     ]),
        # }
        #
        # dataset = {'train': torchvision.datasets.ImageFolder(train_path, data_transforms['train']),
        #            'val': torchvision.datasets.ImageFolder(test_path, data_transforms['val'])}
        #
        # data_loader = {'train': torchdata.DataLoader(dataset['train'], batch_size, num_workers=4,
        #                                              shuffle=True, pin_memory=True),
        #                'val': torchdata.DataLoader(dataset['val'], 24, num_workers=4,
        #                                            shuffle=False, pin_memory=True)}
        #
        # optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9, weight_decay=1e-5)
        # criterion = nn.CrossEntropyLoss()
        # exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=27, gamma=0.1)
        # save_dir = 'IncepResV1'
        # logfile = 'IncepResV1/trainlog.log'
        # trainlog(logfile)
        # best_acc, best_model_wts = train(model,
        #                                  epoch_num,
        #                                  batch_size,
        #                                  start_epoch,
        #                                  optimizer,
        #                                  criterion,
        #                                  exp_lr_scheduler,
        #                                  dataset,
        #                                  data_loader,
        #                                  usecuda,
        #                                  save_inter,
        #                                  save_dir)

    elif isPrune:
        fine_tuner.prune()