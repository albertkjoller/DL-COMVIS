import torch
import numpy as np

import torch.optim as optim
from torchvision import transforms


def set_seed(SEED):
    np.random.seed(SEED)
    torch.manual_seed(SEED)

def invertNormalization(train_mean, train_std):
    return transforms.Compose([
        transforms.Normalize(
            mean=[0., 0., 0.],
            std=[1/0.0214, 1/0.0208, 1/0.0223]
        ),
        transforms.Normalize(
            mean=[-0.5132, -0.4369, -0.3576],
            std=[1., 1., 1.]
        )
    ])


def get_optimizer(optimizer):
    if optimizer == 'Adam':
        return optim.Adam
    elif optimizer == 'SGD':
        return optim.SGD
    else:
        raise ValueError('unknown optimizer')


epsilon = 1e-7

def accuracy(y, y_hat):
    """accuracy of segmentation wrt. ground truth mask"""
    return (y_hat == y).sum().item() / (y.numel() + epsilon)

def specificity(y, y_hat):
    """specificity of segmentation wrt. ground truth mask"""
    return ((y_hat == y) & (y == 0)).sum().item() / ((y == 0).sum().item() + epsilon)

def sensitivity(y, y_hat):
    """sensitivity of segmentation wrt. ground truth mask"""
    return ((y_hat == y) & (y == 1)).sum().item() / ((y == 1).sum().item() + epsilon)

def iou(y, y_hat):
    """intersection over union of segmentation wrt. ground truth mask"""
    return (y_hat & y).sum().item() / ((y_hat | y).sum().item() + epsilon)

def dice_score(y, y_hat):
    """dice coefficient of segmentation wrt. ground truth mask"""
    return 2 * (y_hat & y).sum().item() / ((y_hat.sum().item() + y.sum().item()) + epsilon)

def IoU(y, y_hat):
    """IoU for objection detection, expects bounding boxes
    [x, y, w, h]
    """
    x1 = torch.max(y_hat[:, 0], y[:, 0])
    y1 = torch.max(y_hat[:, 1], y[:, 1])
    x2 = torch.min(y_hat[:, 0] + y_hat[:, 2], y[:, 0] + y[:, 2])
    y2 = torch.min(y_hat[:, 1] + y_hat[:, 3], y[:, 1] + y[:, 3])
    intersection = torch.clamp((x2 - x1), min=0) * torch.clamp((y2 - y1), min=0)
    union = y_hat[:, 2] * y_hat[:, 3] + y[:, 2] * y[:, 3] - intersection
    return intersection / (union + epsilon)
