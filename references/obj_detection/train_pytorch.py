# Copyright (C) 2021, Mindee.

# This program is licensed under the Apache License version 2.
# See LICENSE or go to <https://www.apache.org/licenses/LICENSE-2.0.txt> for full license details.

import os

os.environ['USE_TORCH'] = '1'

import datetime
import logging
import random

import numpy as np
import torch
import torch.optim as optim
import torchvision
from torchvision import transforms
import wandb
from fastprogress.fastprogress import master_bar, progress_bar
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torchvision.ops import MultiScaleRoIAlign

from doctr.datasets import DocArtefacts
from doctr.utils import DetectionMetric


def random_horizontal_flip(img: torch.Tensor, bbox: np.ndarray):
    if bool(random.getrandbits(1)):
        trans = transforms.RandomHorizontalFlip(p=1)
        transformed_ = trans(img)
        bbox_dup = bbox[:]
        for k in range(len(bbox)):
            for id in range(len(bbox[k])):
                bbox_dup[k][id][0], bbox_dup[k][id][2] = 800 - bbox[k][id][2], 800 - bbox[k][id][0]
        return transformed_, bbox_dup
    else:
        return img, bbox


def random_vertical_flip(img: torch.Tensor, bbox: np.ndarray):
    if bool(random.getrandbits(1)):
        trans = transforms.RandomVerticalFlip(p=1.0)
        transformed_ = trans(img)
        bbox_dup = bbox[:]
        for k in range(len(bbox)):
            for id in range(len(bbox[k])):
                bbox_dup[k][id][3], bbox_dup[k][id][1] = 1024 - bbox[k][id][1], 1024 - bbox[k][id][3]
        return (transformed_, bbox_dup)
    else:
        return img, bbox


def convert_to_abs_coords(targets, img_shape, is_transform=False, **kwargs):
    height, width = img_shape[-2:]
    for idx in range(len(targets)):
        targets[idx]['boxes'][:, 0::2] = (targets[idx]['boxes'][:, 0::2] * width).round()
        targets[idx]['boxes'][:, 1::2] = (targets[idx]['boxes'][:, 1::2] * height).round()
    if is_transform:
        bbox = np.array([i["boxes"] for i in targets], dtype=object)
        images = kwargs.get('images', )
        images, bbox = random_vertical_flip(images, bbox)
        images, bbox = random_horizontal_flip(images, bbox)
        targets = [{
            "boxes": torch.from_numpy(bo).to(dtype=torch.float32),
            "labels": torch.tensor(t['labels']).to(dtype=torch.long)}
            for bo, t in zip(bbox, targets)
        ]

        return images, targets
    else:
        targets = [{
            "boxes": torch.from_numpy(t['boxes']).to(dtype=torch.float32),
            "labels": torch.tensor(t['labels']).to(dtype=torch.long)}
            for t in targets
        ]
        return targets


def fit_one_epoch(model, train_loader, optimizer, scheduler, mb, is_transform=True, ):
    model.train()
    train_iter = iter(train_loader)
    # Iterate over the batches of the dataset
    for _ in progress_bar(range(len(train_loader)), parent=mb):
        images, targets = next(train_iter)
        optimizer.zero_grad()
        if is_transform:
            kwargs = {"images": images}
            images, targets = convert_to_abs_coords(targets, images.shape, is_transform=True, **kwargs)
        else:
            targets = convert_to_abs_coords(targets, images.shape)
        if torch.cuda.is_available():
            images = images.cuda()
            targets = [{k: v.cuda() for k, v in t.items()} for t in targets]

        loss_dict = model(images, targets)
        loss = sum(v for v in loss_dict.values())
        loss.backward()
        optimizer.step()
        mb.child.comment = f'Training loss: {loss.item()}'
    scheduler.step()


@torch.no_grad()
def evaluate(model, val_loader, metric):
    model.eval()
    metric.reset()
    val_iter = iter(val_loader)
    for images, targets in val_iter:
        images, targets = next(val_iter)
        # batch_transforms
        targets = convert_to_abs_coords(targets, images.shape)
        if torch.cuda.is_available():
            images = images.cuda()
        output = model(images)
        pred_labels = np.concatenate([o['labels'].cpu().numpy() for o in output])
        pred_boxes = np.concatenate([o['boxes'].cpu().numpy() for o in output])
        gt_boxes = np.concatenate([o['boxes'].cpu().numpy() for o in targets])
        gt_labels = np.concatenate([o['labels'].cpu().numpy() for o in targets])
        metric.update(gt_boxes, pred_boxes, gt_labels, pred_labels)
    recall, precision, mean_iou = metric.summary()
    return recall, precision, mean_iou


def main(args):
    torch.backends.cudnn.benchmark = True

    # Filter keys
    state_dict = {
        k: v for k, v in torchvision.models.detection.__dict__[args.arch](pretrained=True).state_dict().items()
        if not k.startswith('roi_heads.')
    }
    defaults = {"min_size": 700, "max_size": 1300,
                "box_fg_iou_thresh": 0.5,
                "box_bg_iou_thresh": 0.5,
                "box_detections_per_img": 150, "box_score_thresh": 0.15, "box_positive_fraction": 0.35,
                "box_nms_thresh": 0.2,
                "rpn_pre_nms_top_n_train": 2000, "rpn_pre_nms_top_n_test": 1000,
                "rpn_post_nms_top_n_train": 2000, "rpn_post_nms_top_n_test": 1000,
                "rpn_nms_thresh": 0.2,
                "rpn_batch_size_per_image": 250
                }
    kwargs = {**defaults}

    model = torchvision.models.detection.__dict__[args.arch](pretrained=False, num_classes=5, **kwargs)
    model.load_state_dict(state_dict, strict=False)
    model.roi_heads.box_roi_pool = MultiScaleRoIAlign(featmap_names=['0', '1', '2', '3'], output_size=(7, 7),
                                                      sampling_ratio=2)
    anchor_sizes = ((16), (64), (128), (264))
    aspect_ratios = ((0.5, 1.0, 2.0, 3.0,)) * len(anchor_sizes)
    model.rpn.anchor_generator.sizes = anchor_sizes
    model.rpn.anchor_generator.aspect_ratios = aspect_ratios
    # GPU
    if isinstance(args.device, int):
        if not torch.cuda.is_available():
            raise AssertionError("PyTorch cannot access your GPU. Please investigate!")
        if args.device >= torch.cuda.device_count():
            raise ValueError("Invalid device index")
    # Silent default switch to GPU if available
    elif torch.cuda.is_available():
        args.device = 0
    else:
        logging.warning("No accessible GPU, target device set to CPU.")
    if torch.cuda.is_available():
        torch.cuda.set_device(args.device)
        model = model.cuda()
    optimizer = optim.SGD([p for p in model.parameters() if p.requires_grad],
                          lr=args.lr, weight_decay=args.weight_decay)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.1)
    train_set = DocArtefacts(train=True, download=True)
    val_set = DocArtefacts(train=False, download=True)
    train_loader = DataLoader(train_set, batch_size=args.batch_size, num_workers=args.workers,
                              sampler=RandomSampler(train_set), pin_memory=torch.cuda.is_available(),
                              collate_fn=train_set.collate_fn,
                              drop_last=True)
    val_loader = DataLoader(val_set, batch_size=args.batch_size, num_workers=args.workers,
                            sampler=SequentialSampler(val_set), pin_memory=torch.cuda.is_available(),
                            collate_fn=val_set.collate_fn,
                            drop_last=False)

    metric = DetectionMetric(iou_thresh=0.5)
    if args.test_only:
        print("Running evaluation")
        recall, precision, mean_iou = evaluate(model, val_loader, metric)
        print(f"Recall: {recall:.2%} | Precision: {precision:.2%} | IoU: {mean_iou:.2%}")
        return

    # Training monitoring
    current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    exp_name = f"{args.arch}_{current_time}" if args.name is None else args.name

    # W&B
    if args.wb:
        run = wandb.init(
            name=exp_name,
            project="object-detection",
            config={
                "learning_rate": args.lr,
                "epochs": args.epochs,
                "weight_decay": args.weight_decay,
                "batch_size": args.batch_size,
                "architecture": args.arch,
                "input_size": args.input_size,
                "optimizer": "sgd",
                "framework": "pytorch",
                "scheduler": args.sched,
                "pretrained": args.pretrained,
            }
        )

    mb = master_bar(range(args.epochs))
    max_score = 0.

    for epoch in mb:
        fit_one_epoch(model, train_loader, optimizer, scheduler, mb, is_transform=args.use_augmentations)
        recall, precision, mean_iou = evaluate(model, val_loader, metric)
        f1_score = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.

        mb.write(
            f"Epoch {epoch + 1}/{args.epochs} - "
            f"Recall: {recall:.2%} | Precision: {precision:.2%} "
            f"|IoU: {mean_iou:.2%}")
        # W&B
        if args.wb:
            wandb.log({
                'recall': recall,
                'precision': precision,
                'iou': mean_iou,
            })
        if f1_score > max_score:
            print(f"Validation metric increased {max_score:.6} --> {f1_score:.6}: saving state...")
            # torch.save(model.state_dict(), f"./{exp_name}.pt")
            torch.save(model.state_dict(), f"./{epoch}.pt")
            max_score = f1_score

    if args.wb:
        run.finish()


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='DocTR training script for object detection (PyTorch)',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('arch', type=str, help='text-detection model to train')
    parser.add_argument('--name', type=str, default=None, help='Name of your training experiment')
    parser.add_argument('--epochs', type=int, default=20, help='number of epochs to train the model on')
    parser.add_argument('-b', '--batch_size', type=int, default=8, help='batch size for training')
    parser.add_argument('--device', default=None, type=int, help='device')
    parser.add_argument('--use_augmentations', default=True, type=bool, help='augmentations')
    parser.add_argument('--input_size', type=int, default=1024, help='model input size, H = W')
    parser.add_argument('--lr', type=float, default=0.001, help='learning rate for the optimizer (SGD)')
    parser.add_argument('--wd', '--weight-decay', default=0, type=float, help='weight decay', dest='weight_decay')
    parser.add_argument('-j', '--workers', type=int, default=2, help='number of workers used for dataloading')
    parser.add_argument('--resume', type=str, default=None, help='Path to your checkpoint')
    parser.add_argument('--wb', dest='wb', action='store_true',
                        help='Log to Weights & Biases')
    parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                        help='Load pretrained parameters before starting the training')
    parser.add_argument('--sched', type=str, default='cosine', help='scheduler to use')
    parser.add_argument("--test-only", dest='test_only', action='store_true', help="Run the validation loop")
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)
