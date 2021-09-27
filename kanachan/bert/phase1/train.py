#!/usr/bin/env python3

import re
import datetime
import math
import pathlib
import os
from argparse import ArgumentParser
import logging
import json
import sys
from typing import (Optional, Tuple,)
import torch
from torch import backends
from torch import nn
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from torch.distributed import init_process_group
from torch.nn.parallel import DistributedDataParallel
from torch.utils.tensorboard.writer import SummaryWriter
from kanachan import common
from kanachan.iterator_adaptor_base import IteratorAdaptorBase
from kanachan.common import (Dataset,)
from kanachan.bert.encoder import Encoder
from kanachan.bert.phase0.decoder import Decoder
from kanachan.bert.phase0.model import Model
from kanachan.bert.phase0.iterator_adaptor import IteratorAdaptor
from apex.optimizers import (FusedAdam, FusedSGD, FusedLAMB,)
from apex import amp


def _validate(
        config: object, model: Model, is_multiprocess: bool,
        rank: Optional[int]) -> float:
    start_time = datetime.datetime.now()

    # Prepare the validation data loader. Note that this data loader must
    # iterate the validation data set only once.
    iterator_adaptor = lambda fp: IteratorAdaptor(
        fp, config['num_dimensions'], config['dtype'])
    dataset = Dataset(config['validation_data'], iterator_adaptor)
    data_loader = DataLoader(
        dataset, batch_size=config['validation_batch_size'],
        num_workers=config['num_workers'], pin_memory=True,
        drop_last=is_multiprocess)

    with torch.no_grad():
        loss_function = nn.CrossEntropyLoss()
        validation_loss = 0.0
        batch_in_epoch = 0
        for annotation in data_loader:
            batch_size = len(annotation[0])
            if config['device'] != 'cpu':
                if is_multiprocess:
                    world_size = int(os.environ['LOCAL_WORLD_SIZE'])
                    if batch_size % world_size != 0:
                        raise RuntimeError(
                            'Batch size must be divisible by the world size.')
                    first = (batch_size // world_size) * rank
                    last = (batch_size // world_size) * (rank + 1)
                    annotation = tuple(x[first:last].cuda() for x in annotation)
                else:
                    annotation = tuple(x.cuda() for x in annotation)

            prediction = model(annotation[:-1])
            loss = loss_function(prediction, annotation[-1])
            if math.isnan(loss.item()):
                raise RuntimeError('Validation loss becomes NaN.')
            validation_loss += loss.item()

            batch_in_epoch += 1

    validation_loss /= batch_in_epoch

    elapsed_time = datetime.datetime.now() - start_time
    logging.info(f'Validation has finished (elapsed time = {elapsed_time}).')
    logging.info(f'Validation loss = {validation_loss}')

    return validation_loss


def _training_epoch(
        config: object, model: Model, optimizer: Optimizer,
        writer: SummaryWriter, batch: int, epoch: int, batch_in_epoch: int,
        is_main_process: bool, is_multiprocess: bool,
        rank: Optional[int]) -> Tuple[int, int]:
    start_time = datetime.datetime.now()

    # Prepare the training data loader. Note that this data loader must iterate
    # the training data set only once.
    training_iterator_adaptor = lambda fp: IteratorAdaptor(
        fp, config['num_dimensions'], config['dtype'])
    training_dataset = Dataset(
        config['training_data'], training_iterator_adaptor)
    training_data_loader = DataLoader(
        training_dataset, batch_size=config['training_batch_size'],
        num_workers=config['num_workers'], pin_memory=True,
        drop_last=is_multiprocess)

    skipped_batch_in_epoch = 0
    loss_function = nn.CrossEntropyLoss()

    training_loss_file_path \
        = config['snapshots_path'] / f'training_loss.{epoch}.{batch_in_epoch}.txt'
    if training_loss_file_path.exists():
        with open(training_loss_file_path) as f:
            training_loss = float(f.read())
    else:
        training_loss = 0.0

    for annotation in training_data_loader:
        if skipped_batch_in_epoch is not None and skipped_batch_in_epoch < batch_in_epoch:
            skipped_batch_in_epoch += 1
            continue
        skipped_batch_in_epoch = None

        batch_size = len(annotation[0])

        if config['device'] != 'cpu':
            if is_multiprocess:
                world_size = int(os.environ['LOCAL_WORLD_SIZE'])
                if batch_size % world_size != 0:
                    raise RuntimeError(
                        'Batch size must be divisible by the world size.')
                first = (batch_size // world_size) * rank
                last = (batch_size // world_size) * (rank + 1)
                annotation = tuple(x[first:last].cuda() for x in annotation)
            else:
                annotation = tuple(x.cuda() for x in annotation)

        prediction = model(annotation[:-1])
        loss = loss_function(prediction, annotation[-1])
        if math.isnan(loss.item()):
            raise RuntimeError('Training loss becomes NaN.')

        batch_loss = loss.item()
        training_loss += batch_loss

        loss = loss / config['gradient_accumulation_steps']
        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()

        if (batch_in_epoch + 1) % config['gradient_accumulation_steps'] == 0:
            optimizer.step()
            optimizer.zero_grad()

        logging.info(
            f'epoch = {epoch}, batch = {batch_in_epoch}, training batch loss = {batch_loss}')
        if is_main_process:
            writer.add_scalar(
                'Training batch loss', batch_loss, batch + batch_in_epoch)

        batch_in_epoch += 1

        if is_main_process and batch_in_epoch % config['snapshot_interval'] == 0:
            config['snapshots_path'].mkdir(parents=False, exist_ok=True)
            torch.save(
                encoder.state_dict(),
                config['snapshots_path'] / f'encoder.{epoch}.{batch_in_epoch}.pth')
            torch.save(
                decoder.state_dict(),
                config['snapshots_path'] / f'decoder.{epoch}.{batch_in_epoch}.pth')
            torch.save(
                optimizer.state_dict(),
                config['snapshots_path'] / f'optimizer.{epoch}.{batch_in_epoch}.pth')
            training_loss_file_path \
                = config['snapshots_path'] / f'training_loss.{epoch}.{batch_in_epoch}.txt'
            with open(training_loss_file_path, 'w') as f:
                print(training_loss, file=f, end='')

    elapsed_time = datetime.datetime.now() - start_time
    logging.info(f'Training epoch has finished (elapsed time = {elapsed_time}).')
    logging.info(f'Training epoch loss = {training_loss / batch_in_epoch}')

    batch += batch_in_epoch
    if is_main_process:
        config['snapshots_path'].mkdir(parents=False, exist_ok=True)
        with open(config['snapshots_path'] / 'batch.json', 'w') as f:
            data = {'training_batch_size': batch_size, 'batch': batch}
            json.dump(data, f, separators=(',', ':'))

    epoch += 1
    batch_in_epoch = 0

    validation_loss = _validate(config, model, is_multiprocess, rank)
    if is_main_process:
        writer.add_scalar('Validation epoch loss', validation_loss, epoch)

    if is_main_process:
        config['snapshots_path'].mkdir(parents=False, exist_ok=True)
        torch.save(
            encoder.state_dict(),
            config['snapshots_path'] / f'encoder.{epoch}.{batch_in_epoch}.pth')
        torch.save(
            decoder.state_dict(),
            config['snapshots_path'] / f'decoder.{epoch}.{batch_in_epoch}.pth')
        torch.save(
            optimizer.state_dict(),
            config['snapshots_path'] / f'optimizer.{epoch}.{batch_in_epoch}.pth')

    return batch, epoch


if __name__ == '__main__':
    ap = ArgumentParser(
        description='Pre-train BERT by imitating high-level human players.')
    ap_data = ap.add_argument_group(title='Data')
    ap_data.add_argument(
        '--training-data', type=pathlib.Path, required=True,
        help='path to training data', metavar='PATH')
    ap_data.add_argument(
        '--validation-data', type=pathlib.Path, required=True,
        help='path to validation data', metavar='PATH')
    ap_data.add_argument(
        '--num-workers', default=2, type=int,
        help='# of worker processes in data loading (defaults to 2)',
        metavar='NWORKERS')
    ap_device = ap.add_argument_group(title='Device')
    ap_device.add_argument('--device', help='device', metavar='DEV')
    ap_device.add_argument(
        '--dtype', default='float32', choices=('float16','float32'),
        help='floating point type (defaults to `float32`)')
    ap_model = ap.add_argument_group(title='Model')
    ap_model.add_argument(
        '--num-dimensions', default=768, type=int,
        help='# of embedding dimensions (defaults to 768)', metavar='DIM')
    ap_model.add_argument(
        '--num-heads', default=12, type=int, help='# of heads (defaults to 12)',
        metavar='NHEAD')
    ap_model.add_argument(
        '--dim-feedforward', type=int,
        help='dimension of the feedforward network in each layer (defaults to 4 * DIM)',
        metavar='DIM_FEEDFORWARD')
    ap_model.add_argument(
        '--num-layers', default=12, type=int,
        help='# of layers (defaults to 12)', metavar='NLAYERS')
    ap_model.add_argument(
        '--dim-final-feedforward', type=int,
        help='dimension of the final feedforward network (defaults to DIM_FEEDFORWARD)',
        metavar='DIM_FINAL_FEEDFORWARD')
    ap_model.add_argument(
        '--activation-function', default='gelu', choices=('relu', 'gelu',),
        help='activation function for the feedforward networks (defaults to `gelu`)',
        metavar='ACTIVATION')
    ap_model.add_argument(
        '--initial-encoder', type=pathlib.Path,
        help='path to the initial encoder; mutually exclusive to `--resume`',
        metavar='PATH')
    ap_model.add_argument(
        '--initial-decoder', type=pathlib.Path,
        help='path to the initial decoder; mutually exclusive to `--resume`',
        metavar='PATH')
    ap_training = ap.add_argument_group(title='Training')
    ap_training.add_argument(
        '--training-batch-size', default=96, type=int,
        help='training batch size (defaults to 96)', metavar='N')
    ap_training.add_argument(
        '--validation-batch-size', default=1024, type=int,
        help='validation batch size (defaults to 1024)', metavar='N')
    ap_training.add_argument(
        '--optimizer', default='lamb', choices=('adam', 'sgd', 'lamb',),
        help='optimizer (defaults to `lamb`)')
    ap_training.add_argument(
        '--learning-rate', type=float,
        help='learning rate (defaults to 0.001 for `adam` and `lamb`, 0.1 for `sgd`)',
        metavar='LR')
    ap_training.add_argument(
        '--epsilon', default=1.0e-5, type=float,
        help='epsilon parameter; only meaningful for Adam and LAMB (defaults to 1.0e-5)',
        metavar='EPS')
    ap_training.add_argument(
        '--momentum', default=0.9, type=float,
        help='momentum factor; only meaningful for SGD (defaults to 0.9)',
        metavar='MOMENTUM')
    ap_training.add_argument(
        '--dropout', default=0.1, type=float, help='defaults to 0.1',
        metavar='DROPOUT')
    ap_training.add_argument(
        '--gradient-accumulation-steps', default=16, type=int,
        help='# of steps for gradient accumulation (defaults to 16)',
        metavar='NSTEPS')
    ap_training.add_argument(
        '--initial-optimizer', type=pathlib.Path,
        help='path to the initial optimizer state; mutually exclusive to `--resume`',
        metavar='PATH')
    ap_output = ap.add_argument_group(title='Output')
    ap_output.add_argument(
        '--output-prefix', type=pathlib.Path, required=True, metavar='PATH')
    ap_output.add_argument('--experiment-name', metavar='NAME')
    ap_output.add_argument(
        '--snapshot-interval', default=10000, type=int,
        help='take a snapshot every specified number of batches (defaults to 10000)',
        metavar='NBATCHES')
    ap_output.add_argument('--resume', action='store_true')

    config = ap.parse_args()

    if 'LOCAL_RANK' in os.environ:
        if os.environ['WORLD_SIZE'] != os.environ['LOCAL_WORLD_SIZE']:
            raise RuntimeError('Multi-node not supported')
        rank = int(os.environ['LOCAL_RANK'])
        is_main_process = rank == 0
        is_multiprocess = int(os.environ['LOCAL_WORLD_SIZE']) >= 2
        torch.cuda.set_device(rank)
    else:
        rank = None
        is_main_process = True
        is_multiprocess = False

    if not config.training_data.exists():
        raise RuntimeError(f'{config.training_data}: does not exist')
    if not config.validation_data.exists():
        raise RuntimeError(f'{config.validation_data}: does not exist')
    if config.num_workers < 0:
        raise RuntimeError(
            f'{config.num_workers}: invalid number of workers')

    if config.device is not None:
        m = re.search('^(?:cpu)|(?:cuda(\\d+)?)', config.device)
        if m is None:
            raise RuntimeError(f'{config.device}: invalid device')
        if is_multiprocess and m[1] != '':
            raise RuntimeError(
                'Must not specify any device number in multi-process mode')
        device = config.device
    elif backends.cuda.is_built():
        device = 'cuda'
    else:
        device = 'cpu'
    if config.dtype == 'float16':
        dtype = torch.float16
    elif config.dtype == 'float32':
        dtype = torch.float32
    else:
        raise RuntimeError(f'{config.dtype}: invalid value for `--dtype`')

    if config.num_dimensions < 1:
        raise RuntimeError(
            f'{config.num_dimensions}: invalid number of dimensions')
    if config.num_heads < 1:
        raise RuntimeError(f'{config.num_heads}: invalid number of heads')
    if config.dim_feedforward is None:
        config.dim_feedforward = 4 * config.num_dimensions
    if config.dim_feedforward < 1:
        raise RuntimeError(
            f'{config.dim_feedforward}: invalid dimension of the feedfoward network')
    if config.num_layers < 1:
        raise RuntimeError(f'{config.num_layers}: invalid number of layers')
    if config.dim_final_feedforward is None:
        config.dim_final_feedforward = config.dim_feedforward
    if config.dim_final_feedforward < 1:
        raise RuntimeError(
            f'{config.dim_final_feedforward}: invalid dimension of the final feedforward network')
    if config.initial_encoder is not None and not config.initial_encoder.exists():
        raise RuntimeError(f'{config.initial_encoder}: does not exist')
    if config.initial_encoder is not None and config.resume:
        raise RuntimeError(f'`--initial-encoder` conflicts with `--resume`')
    if config.initial_decoder is not None and not config.initial_decoder.exists():
        raise RuntimeError(f'{config.initial_decoder}: does not exist')
    if config.initial_decoder is not None and config.resume:
        raise RuntimeError(f'`--initial-decoder` conflicts with `--resume`')

    if config.training_batch_size < 1:
        raise RuntimeError(
            f'{config.training_batch_size}: invalid training batch size')
    if config.validation_batch_size < 1:
        raise RuntimeError(
            f'{config.validation_batch_size}: invalid validation batch size')
    if config.optimizer == 'sgd':
        if config.momentum == 0.0:
            sparse = True
        else:
            # See https://github.com/pytorch/pytorch/issues/29814
            sparse = False
    else:
        assert(config.optimizer in ('adam', 'lamb',))
        sparse = False
    if config.optimizer in ('adam', 'lamb',):
        if config.learning_rate is None:
            learning_rate = 0.001
        else:
            learning_rate = config.learning_rate
    else:
        assert(config.optimizer == 'sgd')
        if config.learning_rate is None:
            learning_rate = 0.1
        else:
            learning_rate = config.learning_rate
    if config.gradient_accumulation_steps < 1:
        raise RuntimeError(
            f'{config.gradient_accumulation_steps}: invalid steps for gradient accumulation')
    if config.initial_optimizer is not None and not config.initial_optimizer.exists():
        raise RuntimeError(f'{config.initial_optimizer}: does not exist')
    if config.initial_optimizer is not None and config.resume:
        raise RuntimeError(f'`--initial-optimizer` conflicts with `--resume`')

    if config.experiment_name is None:
        now = datetime.datetime.now()
        experiment_name = now.strftime('%Y-%m-%d-%H-%M-%S')
    else:
        experiment_name = config.experiment_name

    experiment_path = pathlib.Path(config.output_prefix / experiment_name)
    if rank is None and (experiment_path / 'training.log').exists() and not config.resume:
        raise RuntimeError(
            f'{experiment_path}: already exists; did you mean `--resume`?')
    if rank == 0 and (experiment_path / 'training.0.log').exists() and not config.resume:
        raise RuntimeError(
            f'{experiment_path}: already exists; did you mean `--resume`?')
    snapshots_path = experiment_path / 'snapshots'
    tensorboard_path = experiment_path / 'tensorboard'

    if config.snapshot_interval < 1:
        raise RuntimeError(
            f'{config.snapshot_interval}: invalid value for `--snapshot-interval`')

    if (snapshots_path / 'batch.json').exists():
        with open(snapshots_path / 'batch.json') as f:
            data = json.load(f)
            if config.training_batch_size != data['training_batch_size']:
                raise RuntimeError(f'{config.training_batch_size}: different\
 from the previous training batch size ({data["training_batch_size"]})')
            batch = data['batch']
    else:
        batch = 0

    experiment_path.mkdir(parents=True, exist_ok=True)
    common.initialize_logging(experiment_path, rank)

    logging.info(f'Training data: {config.training_data}')
    logging.info(f'Validation data: {config.validation_data}')
    logging.info(f'# of workers: {config.num_workers}')
    logging.info(f'Device: {device}')
    if backends.cudnn.is_available():
        logging.info(f'cuDNN: available')
        backends.cudnn.benchmark = True
    else:
        logging.info(f'cuDNN: N/A')
    logging.info(f'dtype: {dtype}')
    logging.info(f'# of dimensions: {config.num_dimensions}')
    logging.info(f'# of heads: {config.num_heads}')
    logging.info(
        f'Dimension of the feedforward network in each layer: {config.dim_feedforward}')
    logging.info(f'# of layers: {config.num_layers}')
    logging.info(
        f'Dimension of the final feedforward network: {config.dim_final_feedforward}')
    logging.info(f'Activation function: {config.activation_function}')
    logging.info(f'Sparse: {sparse}')
    if config.initial_encoder is None and not config.resume:
        logging.info(f'Initial encoder: (initialized randomly)')
    elif config.initial_encoder is not None:
        logging.info(f'Initial encoder: {config.initial_encoder}')
    if config.initial_decoder is None and not config.resume:
        logging.info(f'Initial decoder: (initialized randomly)')
    elif config.initial_decoder is not None:
        logging.info(f'Initial decoder: {config.initial_decoder}')
    logging.info(f'Training batch size: {config.training_batch_size}')
    logging.info(f'Validation batch size: {config.validation_batch_size}')
    logging.info(f'Optimizer: {config.optimizer}')
    logging.info(f'Learning rate: {learning_rate}')
    if config.optimizer in ('adam', 'lamb',):
        logging.info(f'Epsilon parameter: {config.epsilon}')
    if config.optimizer == 'sgd':
        logging.info(f'Momentum factor: {config.momentum}')
    logging.info(
        f'# of steps for gradient accumulation: {config.gradient_accumulation_steps}')
    logging.info(f'Dropout: {config.dropout}')
    if config.initial_optimizer is None and not config.resume:
        logging.info(f'Initial optimizer state: (initialized normally)')
    elif config.initial_optimizer is not None:
        logging.info(f'Initial optimizer state: {config.initial_optimizer}')
    if rank is None:
        logging.info(f'Process rank: N/A (single process)')
    else:
        logging.info(f'Process rank: {rank}')
    logging.info(f'Snapshot interval: {config.snapshot_interval}')
    if config.resume:
        logging.info(f'Resume from {experiment_path}')
    else:
        logging.info(f'Experiment output: {experiment_path}')

    config = {
        'training_data': config.training_data,
        'validation_data': config.validation_data,
        'num_workers': config.num_workers,
        'device': device,
        'dtype': dtype,
        'num_dimensions': config.num_dimensions,
        'num_heads': config.num_heads,
        'dim_feedforward': config.dim_feedforward,
        'num_layers': config.num_layers,
        'dim_final_feedforward': config.dim_final_feedforward,
        'activation_function': config.activation_function,
        'sparse': sparse,
        'initial_encoder': config.initial_encoder,
        'initial_decoder': config.initial_decoder,
        'training_batch_size': config.training_batch_size,
        'validation_batch_size': config.validation_batch_size,
        'optimizer': config.optimizer,
        'learning_rate': learning_rate,
        'epsilon': config.epsilon,
        'momentum': config.momentum,
        'dropout': config.dropout,
        'gradient_accumulation_steps': config.gradient_accumulation_steps,
        'initial_optimizer': config.initial_optimizer,
        'experiment_name': experiment_name,
        'experiment_path': experiment_path,
        'snapshots_path': snapshots_path,
        'tensorboard_path': tensorboard_path,
        'snapshot_interval': config.snapshot_interval,
        'resume': config.resume,
    }

    encoder = Encoder(
        config['num_dimensions'], config['num_heads'],
        config['dim_feedforward'], config['num_layers'],
        dropout=config['dropout'],
        activation_function=config['activation_function'],
        sparse=config['sparse'])
    decoder = Decoder(
        config['num_dimensions'], config['dim_final_feedforward'],
        dropout=config['dropout'],
        activation_function=config['activation_function'])
    model = Model(encoder, decoder)

    if config['initial_encoder'] is not None:
        assert(not config['resume'])
        encoder.load_state_dict(torch.load(config['initial_encoder']))
    if config['initial_decoder'] is not None:
        assert(not config['resume'])
        decoder.load_state_dict(torch.load(config['initial_decoder']))

    epoch = 0
    batch_in_epoch = 0
    if config['resume']:
        assert(config['initial_encoder'] is None)
        assert(config['initial_decoder'] is None)
        if not config['snapshots_path'].exists():
            raise RuntimeError(f'{config["snapshots_path"]}: does not exist')
        for child in os.listdir(config['snapshots_path']):
            m = re.search('^encoder\\.(\\d+)\\.(\\d+)\\.pth$', child)
            if m is None:
                continue
            if int(m[1]) > epoch:
                epoch = int(m[1])
                batch_in_epoch = int(m[2])
            if int(m[1]) == epoch and int(m[2]) > batch_in_epoch:
                batch_in_epoch = int(m[2])
        logging.info(
            f'Resumed from the {epoch}-th epoch, {batch_in_epoch}-th batch')
        latest_encoder_snapshot_path \
            = config['snapshots_path'] / f'encoder.{epoch}.{batch_in_epoch}.pth'
        encoder.load_state_dict(torch.load(latest_encoder_snapshot_path))
        latest_decoder_snapshot_path \
            = config['snapshots_path'] / f'decoder.{epoch}.{batch_in_epoch}.pth'
        decoder.load_state_dict(torch.load(latest_decoder_snapshot_path))

    if config['device'] == 'cpu':
        model.to(dtype=config['dtype'])
    else:
        model.to(device=config['device'], dtype=config['dtype'])

    if config['optimizer'] == 'adam':
        optimizer = FusedAdam(
            model.parameters(), lr=config['learning_rate'],
            eps=config['epsilon'])
    elif config['optimizer'] == 'sgd':
        optimizer = FusedSGD(
            model.parameters(), lr=config['learning_rate'],
            momentum=config['momentum'])
    else:
        assert(config['optimizer'] == 'lamb')
        optimizer = FusedLAMB(
            model.parameters(), lr=config['learning_rate'],
            eps=config['epsilon'])

    if config['initial_optimizer'] is not None:
        assert(not config['resume'])
        optimizer.load_state_dict(torch.load(config['initial_optimizer']))
    if config['resume']:
        assert(config['initial_optimizer'] is None)
        latest_optimizer_snapshot_path \
            = config['snapshots_path'] / f'optimizer.{epoch}.{batch_in_epoch}.pth'
        optimizer.load_state_dict(torch.load(latest_optimizer_snapshot_path))

    if is_main_process:
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
    else:
        model, optimizer = amp.initialize(
            model, optimizer, opt_level='O1', verbosity=0)

    if is_multiprocess:
        init_process_group(backend='nccl')
        model = DistributedDataParallel(
            model, device_ids=[rank], output_device=rank)

    with SummaryWriter(log_dir=config['tensorboard_path']) as writer:
        if batch_in_epoch == 0:
            validation_loss = _validate(config, model, is_multiprocess, rank)
            if is_main_process:
                writer.add_scalar(
                    'Validation epoch loss', validation_loss, epoch)

        while True:
            batch, epoch = _training_epoch(
                config, model, optimizer, writer, batch, epoch, batch_in_epoch,
                is_main_process, is_multiprocess, rank)
            batch_in_epoch = 0