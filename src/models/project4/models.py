import torch
import torch.nn as nn
import torch.nn.functional as F
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateFinder
import pytorch_lightning as pl
import matplotlib.pyplot as plt
import numpy as np
import timm
from torchmetrics.classification import Accuracy
from torchvision.ops import box_iou, nms
from torchmetrics.detection.mean_ap import MeanAveragePrecision
from collections import Counter

from src.utils import accuracy, IoU, plot_SS, Recall

def get_model(model_name, args, loss_fun, optimizer, out=False, num_classes=2, region_size=(512,512)):
    if model_name == 'testnet':
        return TestNet(args, loss_fun, optimizer, out=out, num_classes=num_classes, region_size=region_size)
    elif model_name == 'efficientnet_b4':
        return EfficientNet(args, loss_fun, optimizer, out=out, num_classes=num_classes, region_size=region_size)
    else:
        raise ValueError('unknown model name')



### BASEMODEL ###
class BaseModel(pl.LightningModule):
    '''
    Contains all recurring functionality
    '''
    def __init__(self, args, loss_fun, optimizer, out, num_classes):
        super().__init__()
        self.args = args
        self.lr = self.args.lr
        self.loss_fun = loss_fun
        self.optimizer = optimizer
        self.out = out
        self.offset = 0
        self.num_classes = num_classes
        self.iou_threshold = .5 # TODO: appropriate???
        self.mAP = MeanAveragePrecision()
        self.IoU = IoU
        
        # checkpointing and logging
        self.model_checkpoint = ModelCheckpoint(
            monitor = "val_loss",
            verbose = args.verbose,
            filename = "{epoch}_{val_loss:.4f}",
        )
        
        self.save_hyperparameters(ignore=['loss_fun'])

    def configure_optimizers(self):
        return self.optimizer(self.parameters(), lr = self.args.lr)
        
    def compare_boxes(self, bboxes, cat_ids, pred_bboxes, num_classes):
        # initializing
        num_gt_boxes, num_pred_boxes    = bboxes.shape[0], pred_bboxes.shape[0]
        gt_matches                      = torch.zeros(num_gt_boxes, dtype=torch.bool)
        pred_matches                    = torch.zeros(num_pred_boxes, dtype=torch.bool)
        pred_gt_bboxes                    = -torch.ones(num_pred_boxes, dtype=torch.long)

        # Mark no match as background index (which is num_classes - 1 as defined in data loader)
        pred_labels  = (num_classes - 1) * torch.ones(num_pred_boxes, dtype=torch.long) 
        # Get IoU matrix
        iou         = box_iou(bboxes, pred_bboxes)

        for pred_idx in range(num_pred_boxes):
            # rows are true, columns are how they compare to each estimates
            iou_score = iou[:, pred_idx] # get row

            # check if there are any matches between pred_box and gt_bboxes
            max_iou = torch.max(iou_score)
            if max_iou >= self.iou_threshold:
                # Get index of max score
                gt_idx = torch.argmax(iou_score)
                # Store matches
                gt_matches[gt_idx] = True
                pred_matches[pred_idx] = True
                # for finding the box later on
                pred_gt_bboxes[pred_idx] = gt_idx
                pred_labels[pred_idx] = cat_ids[gt_idx][0]

        return pred_matches, gt_matches, pred_labels, pred_gt_bboxes

    def training_step(self, batch, batch_idx):

        # extract input
        loss, acc = 0, 0

        # for each image
        for (img, cat_ids, bboxes_data, pred_bboxes_data) in batch:
            # for each bounding box
            (bboxes, regions)           = bboxes_data
            (pred_bboxes, pred_regions) = pred_bboxes_data

            # find corresponding gt box
            pred_matches, gt_matches, pred_labels, pred_gt_bboxes = self.compare_boxes(bboxes, cat_ids, pred_bboxes, self.num_classes)
            
            # Downsample background to 25% non-background vs 75% background
            non_background      = pred_labels != (self.num_classes - 1) 
            n_non_background    = non_background.sum().item()
            n_background_sample = (n_non_background + len(regions)) * 3
            # Get subset background idxs
            background_idxs     = np.random.permutation(np.arange(len(pred_labels))[pred_labels == (self.num_classes - 1)])[:n_background_sample]

            # Filter data to subset
            pred_bboxes         = torch.concat([pred_bboxes[non_background], pred_bboxes[background_idxs]])
            pred_labels         = torch.concat([pred_labels[non_background], pred_labels[background_idxs]])
            pred_regions        = torch.concat([pred_regions[non_background], pred_regions[background_idxs]])
            
            all_regions         = torch.concat([pred_regions, regions])            
            all_labels          = torch.concat([pred_labels.to(self.device), cat_ids.flatten().to(self.device)])

            # Classify proposed regions
            y_hat = self.forward(all_regions)

            # Encode data and compute loss
            one_hot_cat_pred    = torch.nn.functional.one_hot(all_labels, num_classes=self.num_classes).to(torch.float)
            loss                += self.loss_fun(y_hat, one_hot_cat_pred)
            acc                 += (y_hat.detach().cpu().argmax(dim=1) == all_labels.detach().cpu()).to(torch.float).mean().item()

        loss /= len(batch)
        acc /= len(batch)

        # Log performance
        self.log('loss/train_step',  loss, batch_size=len(batch), on_step=True, on_epoch=False, prog_bar=True, logger=True)
        self.log('loss/train_epoch', loss, batch_size=len(batch), on_step=False, on_epoch=True, prog_bar=True, logger=True)
        self.log('acc/train_step',  acc, batch_size=len(batch), on_step=True, on_epoch=False, prog_bar=True, logger=True)
        self.log('acc/train_epoch', acc, batch_size=len(batch), on_step=False, on_epoch=True, prog_bar=True, logger=True)

        return loss
    
    def validation_step(self, batch, batch_idx):
        # extract input
        loss, mAP, acc, IoU, Recall = 0, 0, 0, 0, 0
        y_hat = []
        # for each image
        for (img, cat_ids, bboxes_data, pred_bboxes_data) in batch:
            # for each bounding box
            (bboxes, regions)           = bboxes_data
            (pred_bboxes, pred_regions) = pred_bboxes_data
            # find corresponding gt box
            pred_matches, gt_matches, pred_labels, pred_gt_bboxes = self.compare_boxes(bboxes, cat_ids, pred_bboxes, self.num_classes)
            # Classify proposed regions
            y_hat = self.forward(pred_regions)
            # maximum probabilities
            outputs = torch.nn.functional.softmax(y_hat, dim=1)
            pred_prob, pred_cat = torch.max(outputs, 1)

            # Applying NMS (remove redundant boxes)
            keep_indices = nms(pred_bboxes.to(torch.float), pred_prob, self.iou_threshold)
            # Computing AP
            preds = [{'boxes': pred_bboxes[keep_indices], 'scores':pred_prob[keep_indices], 'labels':pred_cat[keep_indices]}]
            targets = [{'boxes': bboxes, 'labels': cat_ids.flatten()}]
            # calculate mAP
            self.mAP.update(preds, targets)
            mAP += self.mAP.compute()['map_50']
            # calculate IoU

            # Label accuracy
            # estimated label and predicted class
            acc += torch.mean((pred_labels[keep_indices]==pred_cat[keep_indices]).to(torch.float))
            # IoU
            IoU += torch.mean(torch.tensor([box_iou(bboxes[0].view(1,-1), pred_bboxes[0].view(1,-1))[0] 
                                            for gt_bbox_idx, pred_bbox in zip(pred_gt_bboxes[keep_indices], pred_bboxes[keep_indices]) 
                                            if gt_bbox_idx !=-1]))
            Recall += torch.mean(torch.tensor([Recall(bboxes[0].view(1,-1), pred_bboxes[0].view(1,-1))[0] 
                                            for gt_bbox_idx, pred_bbox in zip(pred_gt_bboxes[keep_indices], pred_bboxes[keep_indices]) 
                                            if gt_bbox_idx !=-1]))
            # TODO: more metrics?

        # Normalize
        mAP /= len(batch)
        acc /= len(batch)
        IoU /= len(batch)

        # Log performance
        self.log('mAP/val', mAP, batch_size=len(batch), prog_bar=True, logger=True)
        self.log('acc/val', acc, batch_size=len(batch), prog_bar=True, logger=True)
        self.log('IoU/val', IoU, batch_size=len(batch), prog_bar=True, logger=True)
        self.log('Recall/val', IoU, batch_size=len(batch), prog_bar=True, logger=True)

    def test_step(self, batch, batch_idx):
        # extract input
        loss, acc = 0, 0

        # for each image
        for (img, cat_id, bboxes_data, pred_bboxes_data) in batch:
            # for each bounding box
            (bboxes, regions)           = bboxes_data
            (pred_bboxes, pred_regions) = pred_bboxes_data

            # Classify proposed regions
            y_hat = self.forward(pred_regions)

        # Compute performance
        IoU = 1. # TODO: change
        mAP = 1. # TODO: change

        # Log performance
        self.log('IoU/val', IoU, batch_size=len(batch), prog_bar=True, logger=True)
        self.log('mAP/val', mAP, batch_size=len(batch), prog_bar=True, logger=True)

    def predict_step(self, batch, batch_idx):

            # for each image
            for i, (img, cat_id, bboxes_data, pred_bboxes_data) in enumerate(batch):
                # for each bounding box
                (bboxes, regions)           = bboxes_data
                (pred_bboxes, pred_regions) = pred_bboxes_data

                plot_SS(img, pred_bboxes, i, batch_idx)


class TestNet(BaseModel):
    def __init__(self, args, loss_fun, optimizer, out, num_classes, region_size):
        super().__init__(args, loss_fun, optimizer, out, num_classes)
        h, w = region_size
        self.fc1 = nn.Linear(h*w*3, 128)  # 5*5 from image dimension
        self.fc2 = nn.Linear(128, 64)
        self.fc3 = nn.Linear(64, num_classes)
        self.relu = nn.ReLU()
        #self.softmax = nn.Softmax()


    def forward(self, x):
        x = x.view(x.size(0), -1)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.fc2(x)
        x = self.relu(x)
        x = self.fc3(x)
        #x = self.softmax(x)
        return x



### OLD ### have to adjust and remove the train test val steps as they are in the base model


class EfficientNet(BaseModel):
    def __init__(self, args, loss_fun, optimizer, out, num_classes, region_size):
        super().__init__(args, loss_fun, optimizer, out, num_classes)


        # Load model
        self.network = timm.create_model(args.model_name, pretrained=True, num_classes=self.num_classes)
        # num_classes for 28 categories + 1 background
        if args.percentage_to_freeze != -1.0:
            self.freeze_parameters(args.percentage_to_freeze)

    def freeze_parameters(self, percentage_to_freeze):
        # Freeze weights
        if percentage_to_freeze is None:
            print(f"Freezing classification layer ! ")
            for param in self.network.parameters():
                param.requires_grad = False
            
            # Require gradient for classification layer
            self.network.classifier.requires_grad_()

        else:
            total_params = sum(p.numel() for p in self.network.parameters())  # Count total parameters
            params_to_freeze = int(percentage_to_freeze * total_params)  # Calculate number of parameters to freeze

            frozen_params = 0
            non_frozen_params = 0
            for param in self.network.parameters():
                if frozen_params < params_to_freeze:
                    param.requires_grad = False  # Freeze the parameter
                    frozen_params += param.numel()  # Update the count of frozen parameters
                else:
                    non_frozen_params += param.numel()

            print(f"Froze {frozen_params}/{frozen_params + non_frozen_params} = {frozen_params / (frozen_params + non_frozen_params)}%")

    def forward(self, x):
        return self.network(x)

    """
    def training_step(self, batch, batch_idx):
        # Extract and process input
        x, y = batch
        y = torch.nn.functional.one_hot(y, num_classes=self.num_classes).squeeze(1)
        y = y.to(torch.float32)

        # Get prediction, loss and accuracy
        y_hat = self(x)
        loss = self.loss_fun(y_hat, y)
        acc = self.accuracy(y_hat, y)

        # logs metrics for each training_step - [default:True],
        # the average across the epoch, to the progress bar and logger-[default:False]
        self.log("train_acc", acc, on_step=False, on_epoch=True, prog_bar=True, logger=True, sync_dist=True),
        self.log("train_loss", loss, on_step=False, on_epoch=True, prog_bar=True, logger=True)
        return loss

    def validation_step(self, batch, batch_idx):
        # Extract and process input
        x, y = batch
        y = torch.nn.functional.one_hot(y, num_classes=self.num_classes).squeeze(1)
        y = y.to(torch.float32) 

        # Get prediction, loss and accuracy
        y_hat = self(x)
        loss = self.loss_fun(y_hat, y)
        acc = self.accuracy(y_hat, y)

        # logs metrics for each validation_step - [default:False]
        #the average across the epoch - [default:True]
        self.log("val_acc", acc, prog_bar=True, logger=True),
        self.log("val_loss", loss, prog_bar=True, logger=True)
    """
