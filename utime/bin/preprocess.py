"""
Script for running preprocessing on a all or a subset of data described in
a U-Time project directory, loading all selected files with specified:
    - scaler
    - strip function
    - quality control function
    - channel selection
    - re-sampling

Loaded (and processed) files according to those settings are then saved to a single H5 archive for all
specified datasets and dataset splits.

The produced, pre-processed H5 archive of data may be consumed by the 'ut train' script setting flag --preprocessed.

This script should be called form within a U-Time project directory
"""

import numpy as np
import os
import h5py
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from utime import Defaults


def get_argparser():
    """
    Returns an argument parser for this script
    """
    parser = ArgumentParser(description='TODO')  # TODO
    parser.add_argument("--out_path", type=str, required=True,
                        help="Path to output h5 archive")
    parser.add_argument("--dataset_splits", type=str, required=True, nargs='*',
                        help="Dataset splits (e.g. train_data) to preprocess "
                             "and save; space-separated list.")
    parser.add_argument("--overwrite", action='store_true',
                        help='Overwrite previous pre-processed data')
    parser.add_argument("--num_threads", type=int, default=1,
                        help="Number of threads to use for loading and "
                             "writing. Note: HDF5 must be compiled in "
                             "thread-safe mode!")
    return parser


def copy_dataset_hparams(hparams, hparams_out_path):
    groups_to_save = ('select_channels', 'alternative_select_channels',
                      'load_time_channel_sampling_groups')
    with open(hparams_out_path, 'w') as out_f:
        for group in groups_to_save:
            try:
                data = hparams.get_group(group)
            except ValueError:
                continue  # Not found
            else:
                data = data.replace("load_time_channel_sampling_groups",
                                    "access_time_channel_sampling_groups")
                out_f.write(data + "\n")


def add_dataset_entry(hparams_out_path, h5_path,
                      split_identifier, period_length_sec):
    field = "{}_data:\n  " \
            "data_dir: {}\n  " \
            "period_length_sec: {}\n  " \
            "identifier: {}\n\n".format(split_identifier,
                                        h5_path,
                                        period_length_sec,
                                        split_identifier.upper())
    with open(hparams_out_path, "a") as out_f:
        out_f.write(field)


def preprocess_study(h5_file_group, study):
    """
    TODO

    Args:
        h5_file_group:
        study:

    Returns:
        None
    """
    # Create groups
    study_group = h5_file_group.create_group(study.identifier)
    psg_group = study_group.create_group("PSG")
    with study.loaded_in_context():
        X, y = study.get_all_periods()
        for chan_ind, channel_name in enumerate(study.select_channels):
            # Create PSG channel datasets
            psg_group.create_dataset(channel_name.original_name,
                                     data=X[..., chan_ind])
        # Create hypnogram dataset
        study_group.create_dataset("hypnogram", data=y)

        # Create class --> index lookup groups
        cls_to_indx_group = study_group.create_group('class_to_index')
        dtype = np.dtype('uint16') if len(y) <= 65535 else np.dtype('uint32')
        classes = study.hypnogram.classes
        for class_ in classes:
            inds = np.where(y == class_)[0].astype(dtype)
            cls_to_indx_group.create_dataset(
                str(class_), data=inds
            )

        # Set attributes, currently only sample rate is (/may be) used
        study_group.attrs['sample_rate'] = study.sample_rate


def run(args):
    """
    Run the script according to args - Please refer to the argparser.

    args:
        args:    (Namespace)  command-line arguments
    """
    from mpunet.logging import Logger
    from utime.hyperparameters import YAMLHParams
    from utime.utils.scriptutils import assert_project_folder
    from utime.utils.scriptutils import get_splits_from_all_datasets

    project_dir = os.path.abspath("./")
    assert_project_folder(project_dir)

    # Get logger object
    logger = Logger(project_dir + "/preprocessing_logs",
                    active_file='preprocessing',
                    overwrite_existing=args.overwrite,
                    no_sub_folder=True)
    logger("Args dump: {}".format(vars(args)))

    # Load hparams
    hparams = YAMLHParams(Defaults.get_hparams_path(project_dir),
                          logger=logger,
                          no_version_control=True)

    # Initialize and load (potentially multiple) datasets
    datasets = get_splits_from_all_datasets(hparams,
                                            splits_to_load=args.dataset_splits,
                                            logger=logger,
                                            return_data_hparams=True)

    # Check if file exists, and overwrite if specified
    if os.path.exists(args.out_path):
        if args.overwrite:
            os.remove(args.out_path)
        else:
            from sys import exit
            logger("Out file at {} exists, and --overwrite was not set."
                   "".format(args.out_path))
            exit(0)

    # Create dataset hparams output directory
    out_dir = Defaults.get_pre_processed_data_configurations_dir(project_dir)
    if not os.path.exists(out_dir):
        os.mkdir(out_dir)

    with ThreadPoolExecutor(args.num_threads) as pool:
        with h5py.File(args.out_path, "w") as h5_file:
            for dataset, dataset_hparams in datasets:
                # Create a new version of the dataset-specific hyperparameters
                # that contain only the fields needed for pre-processed data
                name = dataset[0].identifier.split("/")[0]
                hparams_out_path = os.path.join(out_dir, name + ".yaml")
                copy_dataset_hparams(dataset_hparams, hparams_out_path)

                # Update paths to dataset hparams in main hparams file
                hparams.set_value(subdir='datasets', name=name,
                                  value=hparams_out_path, overwrite=True)
                # Save the hyperparameters to the pre-processed main hparams
                hparams.save_current(Defaults.get_pre_processed_hparams_path(
                    project_dir
                ))

                # Process each dataset
                for split in dataset:
                    # Add this split to the dataset-specific hparams
                    add_dataset_entry(hparams_out_path,
                                      args.out_path,
                                      split.identifier.split("/")[-1].lower(),
                                      split.period_length_sec)
                    # Overwrite potential load time channel sampler to None
                    split.set_load_time_channel_sampling_groups(None)

                    # Create dataset group
                    split_group = h5_file.create_group(split.identifier)

                    # Run the preprocessing
                    process_func = partial(preprocess_study, split_group)

                    logger.print_to_screen = True
                    logger("Preprocessing dataset:", split)
                    logger.print_to_screen = False
                    n_pairs = len(split.pairs)
                    for i, _ in enumerate(pool.map(process_func,
                                                   split.pairs)):
                        print("  {}/{}".format(i+1, n_pairs),
                              end='\r', flush=True)
                    print("")


def entry_func(args=None):
    # Get the script to execute, parse only first input
    parser = get_argparser()
    args = parser.parse_args(args)
    run(args=args)


if __name__ == "__main__":
    entry_func()
