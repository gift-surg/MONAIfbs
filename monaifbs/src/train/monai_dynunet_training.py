# Copyright 2020 Marta Bianca Maria Ranzini and contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

##
# \file       monai_dynunet_training.py
# \brief      Script to train a dynUNet model in MONAI for automated segmentation
#               Example config file required by the main function is shown in
#               monaifbs/config/monai_dynUnet_training_config.yml
#               Example of model generated by this training function is stored in
#               monaifbs/models/checkpoint_dynUnet_DiceXent.pt
#
# \author     Marta B M Ranzini (marta.ranzini@kcl.ac.uk)
# \date       November 2020
#
# This code was adapted from the dynUNet tutorial in MONAI
# https://github.com/Project-MONAI/tutorials/blob/master/modules/dynunet_tutorial.ipynb

import os
import sys
import logging
import yaml
from datetime import datetime
import argparse
from pathlib import Path

import torch
from torch.nn.functional import interpolate

from torch.utils.tensorboard import SummaryWriter
from monai.config import print_config
from monai.data import DataLoader, PersistentDataset
from monai.utils import misc, set_determinism
from monai.engines import SupervisedTrainer
from monai.networks.nets import DynUNet
from monai.transforms import (
    Compose,
    LoadNiftid,
    AddChanneld,
    CropForegroundd,
    SpatialPadd,
    NormalizeIntensityd,
    RandSpatialCropd,
    RandZoomd,
    RandGaussianNoised,
    RandGaussianSmoothd,
    RandScaleIntensityd,
    RandRotated,
    RandFlipd,
    SqueezeDimd,
    ToTensord,
)

from monai.engines import SupervisedEvaluator
from monai.handlers import (
    LrScheduleHandler,
    StatsHandler,
    CheckpointSaver,
    MeanDice,
    TensorBoardImageHandler,
    TensorBoardStatsHandler,
    ValidationHandler,
    CheckpointLoader
)
from monai.inferers import SimpleInferer

from monaifbs.src.utils.custom_transform import InPlaneSpacingd
from monaifbs.src.utils.custom_losses import DiceCELoss, DiceLossExtended
from monaifbs.src.utils.custom_inferer import SlidingWindowInferer2D

import monaifbs


def create_data_list_of_dictionaries(input_file):
    """
    Convert the list of input files to be processed in the dictionary format needed for MONAI
    Args:
        input_file: path to a .txt or .csv file (with no header) storing two-columns filenames:
            image filename in the first column and segmentation filename in the second column.
            The two columns should be separated by a comma.
    Return
        full_list: list of dicts, storing the filenames input to the MONAI training pipeline
    """
    full_list = []
    with open(input_file, 'r') as data:
        for line in data:
            # remove newline character if present
            line = line.rstrip('\n') if '\n' in line else line
            # split image and segmentation filenames
            try:
                current_f, current_s = line.split(',')
            except ValueError as ve:
                print('ValueError: {} in function create_data_list_of_dictionaries()'.format(ve))
                print("Incorrect format for file {}. A two-column .txt or .csv file (with no header) is expected, "
                      "storing the image filenames in the first column and respective segmentation in "
                      "the second column, separated by a comma. Format of each line:"
                      "/path/to/image.nii.gz,/path/to/seg.nii.gz".format(input_file))
                exit()
            if os.path.isfile(current_f) and os.path.isfile(current_s):
                full_list.append({"image": current_f, "label": current_s})
            else:
                raise FileNotFoundError('Expected image file: {} or segmentation file: {} not found'.format(current_f,
                                                                                                            current_s))
    return full_list


def choose_loss_function(number_out_channels, config_dict):
    """
    Determine what loss function to use based on information in the configuration file.
    Current options are:
        - dynDiceCELoss = Dice + Xent. The Dice is computed per image and per channel in the batch and then average
            across the batch, using smooth terms at numerator and denominator = 1e-5
        - dynDiceCELoss_batch = Batch Dice + Xent. A single Dice value per channel is computed across the whole batch,
             using smooth terms at numerator and denominator = 1e-5
        - Batch_Dice = Batch Dice only, using smooth terms at numerator and denominator = 1e-5
        - Dice_Only = Dice only (per image and per channel, then average across the batch). The smooth term at the
            numerator is set to 0 as it provides greater training stability

    Args:
        number_out_channels: int, determines whether to use sigmoid or softmax as activation
        config_dict: dict, contains configuration parameters for sampling, network and training.
            See monaifbs/config/monai_dynUnet_training_config.yml for an example of the expected fields.

    Return:
        loss_function: callable, selected loss function type.
    """

    # set some parameters for the Dice Loss
    do_sigmoid = True
    do_softmax = False
    if number_out_channels > 1:
        do_sigmoid = False
        do_softmax = True
    pow = 1.0
    if 'pow_dice' in config_dict['training']:
        pow = config_dict['training']['pow_dice']

    # define the loss function based on the indications from the config file
    loss_type = config_dict['training']['loss_type']
    if loss_type == "dynDiceCELoss":
        batch_version = False
        loss_fn = DiceCELoss(pow=pow)
        print("[LOSS] Using DiceCELoss with batch_version={} and Dice^{}\n".format(batch_version, pow))
    elif loss_type == "dynDiceCELoss_batch":
        batch_version = True
        loss_fn = DiceCELoss(batch_version=batch_version, pow=pow)
        print("[LOSS] Using DiceCELoss with batch_version={} and Dice^{}\n".format(batch_version, pow))
    elif loss_type == "Batch_Dice":
        smooth_num = 1e-5
        smooth_den = smooth_num
        batch_version = True
        squared_pred = False
        loss_fn = DiceLossExtended(sigmoid=do_sigmoid, softmax=do_softmax,
                                   smooth_num=smooth_num, smooth_den=smooth_den, squared_pred=squared_pred,
                                   batch_version=batch_version)
        print("[LOSS] Using Dice Loss - BATCH VERSION, "
              "Dice with {} at numerator and {} at denominator, "
              "do_sigmoid={}, do_softmax={}, squared_pred={}, "
              "batch_version={}\n".format(smooth_num, smooth_den, do_sigmoid, do_softmax, squared_pred, batch_version))
    elif loss_type == "Dice_Only":
        smooth_num = 0
        smooth_den = smooth_num
        batch_version = False
        squared_pred = False
        loss_fn = DiceLossExtended(sigmoid=do_sigmoid, softmax=do_softmax,
                                   smooth_num=smooth_num, smooth_den=smooth_den, squared_pred=squared_pred,
                                   batch_version=batch_version)
        print("[LOSS] Using Dice Loss, "
              "Dice with {} at numerator and {} at denominator, "
              "do_sigmoid={}, do_softmax={}, squared_pred={}, "
              "batch_version={}\n".format(smooth_num, smooth_den, do_sigmoid, do_softmax, squared_pred, batch_version))
    else:
        raise IOError("Unrecognized loss type")

    return loss_fn


def run_training(train_file_list, valid_file_list, config_info):
    """
    Pipeline to train a dynUNet segmentation model in MONAI. It is composed of the following main blocks:
        * Data Preparation: Extract the filenames and prepare the training/validation processing transforms
        * Load Data: Load training and validation data to PyTorch DataLoader
        * Network Preparation: Define the network, loss function, optimiser and learning rate scheduler
        * MONAI Evaluator: Initialise the dynUNet evaluator, i.e. the class providing utilities to perform validation
            during training. Attach handlers to save the best model on the validation set. A 2D sliding window approach
            on the 3D volume is used at evaluation. The mean 3D Dice is used as validation metric.
        * MONAI Trainer: Initialise the dynUNet trainer, i.e. the class providing utilities to perform the training loop.
        * Run training: The MONAI trainer is run, performing training and validation during training.
    Args:
        train_file_list: .txt or .csv file (with no header) storing two-columns filenames for training:
            image filename in the first column and segmentation filename in the second column.
            The two columns should be separated by a comma.
            See monaifbs/config/mock_train_file_list_for_dynUnet_training.txt for an example of the expected format.
        valid_file_list: .txt or .csv file (with no header) storing two-columns filenames for validation:
            image filename in the first column and segmentation filename in the second column.
            The two columns should be separated by a comma.
            See monaifbs/config/mock_valid_file_list_for_dynUnet_training.txt for an example of the expected format.
        config_info: dict, contains configuration parameters for sampling, network and training.
            See monaifbs/config/monai_dynUnet_training_config.yml for an example of the expected fields.
    """

    """
    Read input and configuration parameters
    """
    # print MONAI config information
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    print_config()

    # print to log the parameter setups
    print(yaml.dump(config_info))

    # extract network parameters, perform checks/set defaults if not present and print them to log
    if 'seg_labels' in config_info['training'].keys():
        seg_labels = config_info['training']['seg_labels']
    else:
        seg_labels = [1]
    nr_out_channels = len(seg_labels)
    print("Considering the following {} labels in the segmentation: {}".format(nr_out_channels, seg_labels))
    patch_size = config_info["training"]["inplane_size"] + [1]
    print("Considering patch size = {}".format(patch_size))

    spacing = config_info["training"]["spacing"]
    print("Bringing all images to spacing = {}".format(spacing))

    if 'model_to_load' in config_info['training'].keys() and config_info['training']['model_to_load'] is not None:
        model_to_load = config_info['training']['model_to_load']
        if not os.path.exists(model_to_load):
            raise FileNotFoundError("Cannot find model: {}".format(model_to_load))
        else:
            print("Loading model from {}".format(model_to_load))
    else:
        model_to_load = None

    # set up either GPU or CPU usage
    if torch.cuda.is_available():
        print("\n#### GPU INFORMATION ###")
        print("Using device number: {}, name: {}\n".format(torch.cuda.current_device(), torch.cuda.get_device_name()))
        current_device = torch.device("cuda:0")
    else:
        current_device = torch.device("cpu")
        print("Using device: {}".format(current_device))

    # set determinism if required
    if 'manual_seed' in config_info['training'].keys() and config_info['training']['manual_seed'] is not None:
        seed = config_info['training']['manual_seed']
    else:
        seed = None
    if seed is not None:
        print("Using determinism with seed = {}\n".format(seed))
        set_determinism(seed=seed)

    """
    Setup data output directory
    """
    out_model_dir = os.path.join(config_info['output']['out_dir'],
                                 datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '_' +
                                 config_info['output']['out_postfix'])
    print("Saving to directory {}\n".format(out_model_dir))
    # create cache directory to store results for Persistent Dataset
    if 'cache_dir' in config_info['output'].keys():
        out_cache_dir = config_info['output']['cache_dir']
    else:
        out_cache_dir = os.path.join(out_model_dir, 'persistent_cache')
    persistent_cache: Path = Path(out_cache_dir)
    persistent_cache.mkdir(parents=True, exist_ok=True)

    """
    Data preparation
    """
    # Read the input files for training and validation
    print("*** Loading input data for training...")

    train_files = create_data_list_of_dictionaries(train_file_list)
    print("Number of inputs for training = {}".format(len(train_files)))

    val_files = create_data_list_of_dictionaries(valid_file_list)
    print("Number of inputs for validation = {}".format(len(val_files)))

    # Define MONAI processing transforms for the training data. This includes:
    # - Load Nifti files and convert to format Batch x Channel x Dim1 x Dim2 x Dim3
    # - CropForegroundd: Reduce the background from the MR image
    # - InPlaneSpacingd: Perform in-plane resampling to the desired spacing, but preserve the resolution along the
    #       last direction (lowest resolution) to avoid introducing motion artefact resampling errors
    # - SpatialPadd: Pad the in-plane size to the defined network input patch size [N, M] if needed
    # - NormalizeIntensityd: Apply whitening
    # - RandSpatialCropd: Crop a random patch from the input with size [B, C, N, M, 1]
    # - SqueezeDimd: Convert the 3D patch to a 2D one as input to the network (i.e. bring it to size [B, C, N, M])
    # - Apply data augmentation (RandZoomd, RandRotated, RandGaussianNoised, RandGaussianSmoothd, RandScaleIntensityd,
    #       RandFlipd)
    # - ToTensor: convert to pytorch tensor
    train_transforms = Compose(
        [
            LoadNiftid(keys=["image", "label"]),
            AddChanneld(keys=["image", "label"]),
            CropForegroundd(keys=["image", "label"], source_key="image"),
            InPlaneSpacingd(
                keys=["image", "label"],
                pixdim=spacing,
                mode=("bilinear", "nearest"),
            ),
            SpatialPadd(keys=["image", "label"], spatial_size=patch_size,
                        mode=["constant", "edge"]),
            NormalizeIntensityd(keys=["image"], nonzero=False, channel_wise=True),
            RandSpatialCropd(keys=["image", "label"], roi_size=patch_size, random_size=False),
            SqueezeDimd(keys=["image", "label"], dim=-1),
            RandZoomd(
                keys=["image", "label"],
                min_zoom=0.9,
                max_zoom=1.2,
                mode=("bilinear", "nearest"),
                align_corners=(True, None),
                prob=0.16,
            ),
            RandRotated(keys=["image", "label"], range_x=90, range_y=90, prob=0.2,
                        keep_size=True, mode=["bilinear", "nearest"],
                        padding_mode=["zeros", "border"]),
            RandGaussianNoised(keys=["image"], std=0.01, prob=0.15),
            RandGaussianSmoothd(
                keys=["image"],
                sigma_x=(0.5, 1.15),
                sigma_y=(0.5, 1.15),
                sigma_z=(0.5, 1.15),
                prob=0.15,
            ),
            RandScaleIntensityd(keys=["image"], factors=0.3, prob=0.15),
            RandFlipd(["image", "label"], spatial_axis=[0, 1], prob=0.5),
            ToTensord(keys=["image", "label"]),
        ]
    )

    # Define MONAI processing transforms for the validation data
    # - Load Nifti files and convert to format Batch x Channel x Dim1 x Dim2 x Dim3
    # - CropForegroundd: Reduce the background from the MR image
    # - InPlaneSpacingd: Perform in-plane resampling to the desired spacing, but preserve the resolution along the
    #       last direction (lowest resolution) to avoid introducing motion artefact resampling errors
    # - SpatialPadd: Pad the in-plane size to the defined network input patch size [N, M] if needed
    # - NormalizeIntensityd: Apply whitening
    # - ToTensor: convert to pytorch tensor
    # NOTE: The validation data is kept 3D as a 2D sliding window approach is used throughout the volume at inference
    val_transforms = Compose(
        [
            LoadNiftid(keys=["image", "label"]),
            AddChanneld(keys=["image", "label"]),
            CropForegroundd(keys=["image", "label"], source_key="image"),
            InPlaneSpacingd(
                keys=["image", "label"],
                pixdim=spacing,
                mode=("bilinear", "nearest"),
            ),
            SpatialPadd(keys=["image", "label"], spatial_size=patch_size, mode=["constant", "edge"]),
            NormalizeIntensityd(keys=["image"], nonzero=False, channel_wise=True),
            ToTensord(keys=["image", "label"]),
        ]
    )

    """
    Load data 
    """
    # create training data loader
    train_ds = PersistentDataset(data=train_files, transform=train_transforms,
                                 cache_dir=persistent_cache)
    train_loader = DataLoader(train_ds,
                              batch_size=config_info['training']['batch_size_train'],
                              shuffle=True,
                              num_workers=config_info['device']['num_workers'])
    check_train_data = misc.first(train_loader)
    print("Training data tensor shapes:")
    print("Image = {}; Label = {}".format(check_train_data["image"].shape, check_train_data["label"].shape))

    # create validation data loader
    if config_info['training']['batch_size_valid'] != 1:
        raise Exception("Batch size different from 1 at validation ar currently not supported")
    val_ds = PersistentDataset(data=val_files, transform=val_transforms, cache_dir=persistent_cache)
    val_loader = DataLoader(val_ds,
                            batch_size=1,
                            shuffle=False,
                            num_workers=config_info['device']['num_workers'])
    check_valid_data = misc.first(val_loader)
    print("Validation data tensor shapes (Example):")
    print("Image = {}; Label = {}\n".format(check_valid_data["image"].shape, check_valid_data["label"].shape))

    """
    Network preparation
    """
    print("*** Preparing the network ...")
    # automatically extracts the strides and kernels based on nnU-Net empirical rules
    spacings = spacing[:2]
    sizes = patch_size[:2]
    strides, kernels = [], []
    while True:
        spacing_ratio = [sp / min(spacings) for sp in spacings]
        stride = [2 if ratio <= 2 and size >= 8 else 1 for (ratio, size) in zip(spacing_ratio, sizes)]
        kernel = [3 if ratio <= 2 else 1 for ratio in spacing_ratio]
        if all(s == 1 for s in stride):
            break
        sizes = [i / j for i, j in zip(sizes, stride)]
        spacings = [i * j for i, j in zip(spacings, stride)]
        kernels.append(kernel)
        strides.append(stride)
    strides.insert(0, len(spacings) * [1])
    kernels.append(len(spacings) * [3])

    # initialise the network
    net = DynUNet(
        spatial_dims=2,
        in_channels=1,
        out_channels=nr_out_channels,
        kernel_size=kernels,
        strides=strides,
        upsample_kernel_size=strides[1:],
        norm_name="instance",
        deep_supervision=True,
        deep_supr_num=2,
        res_block=False,
    ).to(current_device)
    print(net)

    # define the loss function
    loss_function = choose_loss_function(nr_out_channels, config_info)

    # define the optimiser and the learning rate scheduler
    opt = torch.optim.SGD(net.parameters(), lr=float(config_info['training']['lr']), momentum=0.95)
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        opt, lr_lambda=lambda epoch: (1 - epoch / config_info['training']['nr_train_epochs']) ** 0.9
    )

    """
    MONAI evaluator
    """
    print("*** Preparing the dynUNet evaluator engine...\n")
    # val_post_transforms = Compose(
    #     [
    #         Activationsd(keys="pred", sigmoid=True),
    #     ]
    # )
    val_handlers = [
        StatsHandler(output_transform=lambda x: None),
        TensorBoardStatsHandler(log_dir=os.path.join(out_model_dir, "valid"),
                                output_transform=lambda x: None,
                                global_epoch_transform=lambda x: trainer.state.iteration),
        CheckpointSaver(save_dir=out_model_dir, save_dict={"net": net, "opt": opt}, save_key_metric=True,
                        file_prefix='best_valid'),
    ]
    if config_info['output']['val_image_to_tensorboad']:
        val_handlers.append(TensorBoardImageHandler(log_dir=os.path.join(out_model_dir, "valid"),
                                                    batch_transform=lambda x: (x["image"], x["label"]),
                                                    output_transform=lambda x: x["pred"], interval=2))

    # Define customized evaluator
    class DynUNetEvaluator(SupervisedEvaluator):
        def _iteration(self, engine, batchdata):
            inputs, targets = self.prepare_batch(batchdata)
            inputs, targets = inputs.to(engine.state.device), targets.to(engine.state.device)
            flip_inputs_1 = torch.flip(inputs, dims=(2,))
            flip_inputs_2 = torch.flip(inputs, dims=(3,))
            flip_inputs_3 = torch.flip(inputs, dims=(2, 3))

            def _compute_pred():
                pred = self.inferer(inputs, self.network)
                # use random flipping as data augmentation at inference
                flip_pred_1 = torch.flip(self.inferer(flip_inputs_1, self.network), dims=(2,))
                flip_pred_2 = torch.flip(self.inferer(flip_inputs_2, self.network), dims=(3,))
                flip_pred_3 = torch.flip(self.inferer(flip_inputs_3, self.network), dims=(2, 3))
                return (pred + flip_pred_1 + flip_pred_2 + flip_pred_3) / 4

            # execute forward computation
            self.network.eval()
            with torch.no_grad():
                if self.amp:
                    with torch.cuda.amp.autocast():
                        predictions = _compute_pred()
                else:
                    predictions = _compute_pred()
            return {"image": inputs, "label": targets, "pred": predictions}

    evaluator = DynUNetEvaluator(
        device=current_device,
        val_data_loader=val_loader,
        network=net,
        inferer=SlidingWindowInferer2D(roi_size=patch_size, sw_batch_size=4, overlap=0.0),
        post_transform=None,
        key_val_metric={
            "Mean_dice": MeanDice(
                include_background=False,
                to_onehot_y=True,
                mutually_exclusive=True,
                output_transform=lambda x: (x["pred"], x["label"]),
            )
        },
        val_handlers=val_handlers,
        amp=False,
    )

    """
    MONAI trainer
    """
    print("*** Preparing the dynUNet trainer engine...\n")
    # train_post_transforms = Compose(
    #     [
    #         Activationsd(keys="pred", sigmoid=True),
    #     ]
    # )

    validation_every_n_epochs = config_info['training']['validation_every_n_epochs']
    epoch_len = len(train_ds) // train_loader.batch_size
    validation_every_n_iters = validation_every_n_epochs * epoch_len

    # define event handlers for the trainer
    writer_train = SummaryWriter(log_dir=os.path.join(out_model_dir, "train"))
    train_handlers = [
        LrScheduleHandler(lr_scheduler=scheduler, print_lr=True),
        ValidationHandler(validator=evaluator, interval=validation_every_n_iters, epoch_level=False),
        StatsHandler(tag_name="train_loss", output_transform=lambda x: x["loss"]),
        TensorBoardStatsHandler(summary_writer=writer_train,
                                log_dir=os.path.join(out_model_dir, "train"), tag_name="Loss",
                                output_transform=lambda x: x["loss"],
                                global_epoch_transform=lambda x: trainer.state.iteration),
        CheckpointSaver(save_dir=out_model_dir, save_dict={"net": net, "opt": opt},
                        save_final=True,
                        save_interval=2, epoch_level=True,
                        n_saved=config_info['output']['max_nr_models_saved']),
    ]
    if model_to_load is not None:
        train_handlers.append(CheckpointLoader(load_path=model_to_load, load_dict={"net": net, "opt": opt}))

    # define customized trainer
    class DynUNetTrainer(SupervisedTrainer):
        def _iteration(self, engine, batchdata):
            inputs, targets = self.prepare_batch(batchdata)
            inputs, targets = inputs.to(engine.state.device), targets.to(engine.state.device)

            def _compute_loss(preds, label):
                labels = [label] + [interpolate(label, pred.shape[2:]) for pred in preds[1:]]
                return sum([0.5 ** i * self.loss_function(p, l) for i, (p, l) in enumerate(zip(preds, labels))])

            self.network.train()
            self.optimizer.zero_grad()
            if self.amp and self.scaler is not None:
                with torch.cuda.amp.autocast():
                    predictions = self.inferer(inputs, self.network)
                    loss = _compute_loss(predictions, targets)
                self.scaler.scale(loss).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()
            else:
                predictions = self.inferer(inputs, self.network)
                loss = _compute_loss(predictions, targets).mean()
                loss.backward()
                self.optimizer.step()
            return {"image": inputs, "label": targets, "pred": predictions, "loss": loss.item()}

    trainer = DynUNetTrainer(
        device=current_device,
        max_epochs=config_info['training']['nr_train_epochs'],
        train_data_loader=train_loader,
        network=net,
        optimizer=opt,
        loss_function=loss_function,
        inferer=SimpleInferer(),
        post_transform=None,
        key_train_metric=None,
        train_handlers=train_handlers,
        amp=False,
    )

    """
    Run training
    """
    print("*** Run training...")
    trainer.run()
    print("Done!")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Run training with dynUnet with MONAI.')
    parser.add_argument('--train_files_list',
                        dest='train_files_list',
                        metavar='/path/to/train_files_list.txt',
                        type=str,
                        help='two-column .txt or .csv file (with no header) containing image filenames and associated '
                             'label filenames for training (image-label filenames should be comma separated). '
                             'Expected format of each line:'
                             '/path/to/train_image.nii.gz,/path/to/train_label.nii.gz '
                             'See monaifbs/config/mock_train_file_list_for_dynUnet_training.txt as an example',
                        required=True)
    parser.add_argument('--validation_files_list',
                        dest='valid_files_list',
                        metavar='/path/to/valid_files_list.txt',
                        type=str,
                        help='two-column .txt or .csv file (with no header) containing image filenames and associated '
                             'label filenames for validation (image-label filenames should be comma separated). '
                             'Expected format of each line:'
                             '/path/to/valid_image.nii.gz,/path/to/valid_label.nii.gz '
                             'See monaifbs/config/mock_valid_file_list_for_dynUnet_training.txt as an example',
                        required=True)
    parser.add_argument('--out_folder',
                        dest='out_folder',
                        metavar='/path/to/out_folder',
                        type=str,
                        help='directory where to store the outputs of the training',
                        required=True)
    parser.add_argument('--out_postfix',
                        dest='out_postfix',
                        metavar='out_postfix',
                        type=str,
                        help='postfix to add to the output directory name after datetime stamp',
                        default='monai_dynUnet_2D')
    parser.add_argument('--cache_dir',
                        dest='cache_dir',
                        metavar='/path/to/cache_dir',
                        type=str,
                        help='Directory where preprocessed data are/will be stored. ' 
                             'See MONAI PersistentCacheDataset for more information'
                             'https://github.com/Project-MONAI/MONAI/blob/releases/0.3.0/monai/data/dataset.py '
                             'If not provided, it will be created in /path/to/out_folder/persistent_cache',
                        default=None)
    parser.add_argument('--config_file',
                        dest='config_file',
                        metavar='/path/to/config_file.yml',
                        type=str,
                        help='config file containing network information for training '
                             'The file monaifbs/config/monai_dynUnet_training_config.yml is used by default. '
                             'See that file as an example of the expected structure',
                        default=None)
    args = parser.parse_args()

    # check existence of filenames listing the input data
    if not os.path.isfile(args.train_files_list) or os.path.getsize(args.train_files_list) == 0:
        raise FileNotFoundError('Expected training file {} not found or empty'.format(args.train_files_list))
    if not os.path.isfile(args.valid_files_list) or os.path.getsize(args.valid_files_list) == 0:
        raise FileNotFoundError('Expected validation file {} not found or empty'.format(args.valid_files_list))

    # check existence of config file and read it
    config_file = args.config_file
    if config_file is None:
        config_file = os.path.join(*[os.path.dirname(monaifbs.__file__),
                                     "config", "monai_dynUnet_training_config.yml"])
    if not os.path.isfile(config_file):
        raise FileNotFoundError('Expected config file: {} not found'.format(config_file))
    with open(config_file) as f:
        print("*** Config file")
        print(config_file)
        config = yaml.load(f, Loader=yaml.FullLoader)

    # add the output directory to the config dictionary
    config['output']['out_postfix'] = args.out_postfix
    config['output']['out_dir'] = args.out_folder
    if not os.path.exists(config['output']['out_dir']):
        os.makedirs(config['output']['out_dir'])
    if args.cache_dir is not None:
        config['output']['cache_dir'] = args.cache_dir

    # run training with MONAI dynUnet
    run_training(args.train_files_list, args.valid_files_list, config)

