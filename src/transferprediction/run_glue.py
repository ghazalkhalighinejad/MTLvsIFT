# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning the library models for sequence classification on GLUE (Bert, XLM, XLNet, RoBERTa, Albert, XLM-RoBERTa)."""

# it thinks torch.tensor() is not callable
# pylint: disable=not-callable

import argparse
import glob
import json
import logging
import os
import random
import typing

import numpy as np
import pandas as pd
import torch
from torch.utils.data import (
    DataLoader,
    RandomSampler,
    SequentialSampler,
    TensorDataset,
)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from transformers import (
    WEIGHTS_NAME,
    AdamW,
    AlbertConfig,
    AlbertForSequenceClassification,
    AlbertTokenizer,
    BertConfig,
    BertForSequenceClassification,
    BertTokenizer,
    DistilBertConfig,
    DistilBertForSequenceClassification,
    DistilBertTokenizer,
    FlaubertConfig,
    FlaubertForSequenceClassification,
    FlaubertTokenizer,
    RobertaConfig,
    RobertaForSequenceClassification,
    RobertaTokenizer,
    XLMConfig,
    XLMForSequenceClassification,
    XLMRobertaConfig,
    XLMRobertaForSequenceClassification,
    XLMRobertaTokenizer,
    XLMTokenizer,
    XLNetConfig,
    XLNetForSequenceClassification,
    XLNetTokenizer,
    get_linear_schedule_with_warmup,
)
from transformers import (
    glue_convert_examples_to_features as convert_examples_to_features,
)

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter

from src.transferprediction.huggingface_extensions import (
    RobertaForSequenceClassificationGLUE,
    BertForSequenceClassificationGLUE,
)
from src.transferprediction.multi_dataloader import MTLDataset, MTLRandomSampler

from src.transferprediction.utils import (
    processors,
    output_modes,
    tasks_num_labels,
    compute_metrics,
)

logger = logging.getLogger(__name__)

ALL_MODELS = sum(
    (
        tuple(conf.pretrained_config_archive_map.keys())
        for conf in (
            BertConfig,
            XLNetConfig,
            XLMConfig,
            RobertaConfig,
            DistilBertConfig,
            AlbertConfig,
            XLMRobertaConfig,
            FlaubertConfig,
        )
    ),
    (),
)

MODEL_CLASSES = {
    "bert": (BertConfig, BertForSequenceClassificationGLUE, BertTokenizer),
    "xlnet": (XLNetConfig, XLNetForSequenceClassification, XLNetTokenizer),
    "xlm": (XLMConfig, XLMForSequenceClassification, XLMTokenizer),
    "roberta": (
        RobertaConfig,
        RobertaForSequenceClassificationGLUE,
        RobertaTokenizer,
    ),
    "distilbert": (
        DistilBertConfig,
        DistilBertForSequenceClassification,
        DistilBertTokenizer,
    ),
    "albert": (AlbertConfig, AlbertForSequenceClassification, AlbertTokenizer),
    "xlmroberta": (
        XLMRobertaConfig,
        XLMRobertaForSequenceClassification,
        XLMRobertaTokenizer,
    ),
    "flaubert": (
        FlaubertConfig,
        FlaubertForSequenceClassification,
        FlaubertTokenizer,
    ),
}


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def save_model(args, model, tokenizer, optimizer, scheduler, global_step):
    output_dir = os.path.join(
        args.output_dir, "checkpoint-{}".format(global_step)
    )
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    model_to_save = (
        model.module if hasattr(model, "module") else model
    )  # Take care of distributed/parallel training
    model_to_save.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)

    torch.save(args, os.path.join(output_dir, "training_args.bin"))
    logger.info("Saving model checkpoint to %s", output_dir)

    torch.save(
        optimizer.state_dict(),
        os.path.join(output_dir, "optimizer.pt"),
    )
    torch.save(
        scheduler.state_dict(),
        os.path.join(output_dir, "scheduler.pt"),
    )
    logger.info(
        "Saving optimizer and scheduler states to %s",
        output_dir,
    )


def train(args, train_dataset, model, tokenizer):
    """Train the model"""
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    def collate_func(batch: typing.List):
        """
        Here we see if the instances are already grouped by task.  If they are, batch them for speed
        in processing.  If they're not, create a new sub-batch for that task.

        Returns:
            A list of lists where each list is instances for a task
        """
        batches_by_task = [[] for i in range(len(args.data_dir_list))]
        for instance in batch:
            cur_index = instance[-1]
            batches_by_task[cur_index].append(instance)

        # if by size, it may be zero from one task
        batch_lens = [len(cur_batch) for cur_batch in batches_by_task]
        if sum(batch_lens) == 0:
            raise Exception("Empty task lists")
        elif 0 in batch_lens:
            batches_by_task = [
                cur_batch
                for cur_batch in batches_by_task
                if len(cur_batch) != 0
            ]

        # group them in their lists using the detault collate func
        final_batches = [
            torch.utils.data.dataloader.default_collate(batch)
            for batch in batches_by_task
        ]
        return final_batches

    train_sampler = (
        MTLRandomSampler(train_dataset) if args.local_rank == -1 else None
    )  # TODO: maybe add distributed capabilities

    if (
        "dnc" in args.output_dir or "size_experiment" in args.output_dir
    ):  # dnc has the same number of classes for all
        print("Creating a denser dataloader...")
        train_dataloader = DataLoader(
            train_dataset,
            sampler=train_sampler,
            batch_size=args.train_batch_size,
            collate_fn=lambda x: [
                torch.utils.data.dataloader.default_collate(x)
            ],
        )
    else:
        print("Using split dataloaders...")
        train_dataloader = DataLoader(
            train_dataset,
            sampler=train_sampler,
            batch_size=args.train_batch_size,
            collate_fn=collate_func,
        )

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = (
            args.max_steps
            // (len(train_dataloader) // args.gradient_accumulation_steps)
            + 1
        )
    else:
        t_total = (
            len(train_dataloader)
            // args.gradient_accumulation_steps
            * args.num_train_epochs
        )

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay)
            ],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]

    optimizer = AdamW(
        optimizer_grouped_parameters,
        lr=args.learning_rate,
        eps=args.adam_epsilon,
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=args.warmup_steps,
        num_training_steps=t_total,
    )

    # Check if saved optimizer or scheduler states exist
    if (
        os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt"))
        and os.path.isfile(
            os.path.join(args.model_name_or_path, "scheduler.pt")
        )
        and not args.start_again
    ):
        print("Loading saved optimizer and scheduler")
        # Load in optimizer and scheduler states
        optimizer.load_state_dict(
            torch.load(os.path.join(args.model_name_or_path, "optimizer.pt"))
        )
        scheduler.load_state_dict(
            torch.load(os.path.join(args.model_name_or_path, "scheduler.pt"))
        )

    # if args.fp16:
    #     try:
    #         from apex import amp
    #     except ImportError:
    #         raise ImportError(
    #             "Please install apex from https://www.github.com/nvidia/apex to use fp16 training."
    #         )
    #     model, optimizer = amp.initialize(
    #         model, optimizer, opt_level=args.fp16_opt_level
    #     )

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            find_unused_parameters=True,
        )

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info(
        "  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size
    )
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
        * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info(
        "  Gradient Accumulation steps = %d", args.gradient_accumulation_steps
    )
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    epochs_trained = 0
    steps_trained_in_current_epoch = 0
    # Check if continuing training from a checkpoint
    if os.path.exists(args.model_name_or_path) and not args.start_again:
        # set global_step to gobal_step of last saved checkpoint from model path
        global_step = int(args.model_name_or_path.split("-")[-1].split("/")[0])
        epochs_trained = global_step // (
            len(train_dataloader) // args.gradient_accumulation_steps
        )
        steps_trained_in_current_epoch = global_step % (
            len(train_dataloader) // args.gradient_accumulation_steps
        )

        logger.info(
            "  Continuing training from checkpoint, will skip to saved global_step"
        )
        logger.info("  Continuing training from epoch %d", epochs_trained)
        logger.info("  Continuing training from global step %d", global_step)
        logger.info(
            "  Will skip the first %d steps in the first epoch",
            steps_trained_in_current_epoch,
        )

    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(
        epochs_trained,
        int(args.num_train_epochs),
        desc="Epoch",
        disable=args.local_rank not in [-1, 0],
    )
    if not os.path.isdir(args.output_dir):
        os.makedirs(args.output_dir)
    num_instances = 0
    for epoch in train_iterator:
        in_epoch_iterator = tqdm(
            train_dataloader,
            desc="Iteration",
            disable=args.local_rank not in [-1, 0],
        )

        if args.sampling_type == "dynamic":
            # HACK: this is not very clean, but it does work
            results = evaluate(args, model, tokenizer)
            train_dataloader.dataset.evaluation_metrics = results
            train_dataloader.dataset.old_evaluation_metrics = False

        model.zero_grad()
        all_losses = []
        half_of_batch = len(in_epoch_iterator) // 2
        for step, batch_all in enumerate(in_epoch_iterator):
            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            if step == half_of_batch:
                print("Saving model at half of epoch...")
                save_model(
                    args, model, tokenizer, optimizer, scheduler, global_step
                )

            loss = None
            for batch_with_task in batch_all:
                batch, task_index = batch_with_task[:-1], batch_with_task[-1]
                model.train()

                # the num labels decides on classification or regression
                if task_index.dim() > 1:
                    task_index = task_index.squeeze(0)
                all_tasks_in_batch = list(set(task_index.tolist()))
                # assert len(all_tasks_in_batch) == 1, "batch had different tasks"
                model.num_labels = args.num_label_list[all_tasks_in_batch[0]]

                batch = tuple(t.to(args.device) for t in batch)
                # inputs should be dim=2 (batch_size, seq_len) for input_ids etc. and dim=1 for labels
                inputs = {
                    "input_ids": batch[0].squeeze(1)
                    if batch[0].dim() > 2
                    else batch[0],
                    "attention_mask": batch[1].squeeze(1)
                    if batch[1].dim() > 2
                    else batch[1],
                    "labels": batch[3].squeeze(0)
                    if batch[3].dim() > 1
                    else batch[3],
                }
                if args.model_type != "distilbert":
                    tokens = (
                        batch[2].squeeze(1) if batch[2].dim() > 2 else batch[2]
                    )
                    inputs["token_type_ids"] = (
                        tokens
                        if args.model_type in ["bert", "xlnet", "albert"]
                        else None
                    )
                    # XLM, DistilBERT, RoBERTa, and XLM-RoBERTa don't use segment_ids
                num_instances += inputs["input_ids"].shape[0]
                outputs = model(**inputs)
                sub_loss = outputs[
                    0
                ]  # model outputs are always tuple in transformers (see doc)

                if args.n_gpu > 1:
                    loss = (
                        loss.mean()
                    )  # mean() to average on multi-gpu parallel training
                loss = sub_loss if loss is None else loss + sub_loss

            # only do the rest every batch
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(
                        amp.master_params(optimizer), args.max_grad_norm
                    )
                else:
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), args.max_grad_norm
                    )
                all_losses.append(loss.item())
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

            if args.max_steps > 0 and global_step > args.max_steps:
                in_epoch_iterator.close()
                break

        if args.local_rank in [-1, 0]:
            # Save model checkpoint every epoch
            # want to get a fairly accurate estimate
            end_of_epoch_loss = np.array(all_losses[-10:]).mean()
            all_losses = []
            with open(
                os.path.join(args.output_dir, "loss_values.txt"), "a"
            ) as fout:
                fout.write("epoch {} loss {}".format(epoch, end_of_epoch_loss))
                fout.write("\n")
            print("Saving model at end of epoch...")
            save_model(
                args, model, tokenizer, optimizer, scheduler, global_step
            )

        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def get_grad_of_loss(model) -> torch.tensor:
    list_of_grads = []
    for n, p in model.named_parameters():
        if p.grad is not None:
            list_of_grads.append(p.grad.view(-1))

    mean_grad_of_loss = torch.cat(list_of_grads).mean()
    return mean_grad_of_loss


def evaluate(args, model, tokenizer, prefix=""):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_task_name_list = []
    eval_output_dirs_list = []
    for task_name in args.task_names:
        eval_task_names = ("mnli",) if task_name == "mnli" else (task_name,)
        eval_outputs_dirs = (
            (args.output_dir, args.output_dir + "-MM")
            if task_name == "mnli"
            else (args.output_dir,)
        )
        eval_output_dirs_list.append(eval_outputs_dirs)
        eval_task_name_list.append(eval_task_names)

    full_results = {}
    for eval_task_index, (eval_task_names, eval_outputs_dirs) in enumerate(
        zip(eval_task_name_list, eval_output_dirs_list)
    ):
        results = {}
        for index, (eval_task, eval_output_dir) in enumerate(
            zip(eval_task_names, eval_outputs_dirs)
        ):
            output_mode = output_modes[eval_task]
            eval_dataset = load_and_cache_examples(
                args,
                args.task_list[eval_task_index],
                args.data_dir_list[eval_task_index],
                tokenizer,
                evaluate=True,
                shorten=args.short,
            )

            if not os.path.exists(eval_output_dir) and args.local_rank in [
                -1,
                0,
            ]:
                os.makedirs(eval_output_dir)

            args.eval_batch_size = args.per_gpu_eval_batch_size * max(
                1, args.n_gpu
            )
            # Note that DistributedSampler samples randomly
            eval_sampler = SequentialSampler(eval_dataset)
            eval_dataloader = DataLoader(
                eval_dataset,
                sampler=eval_sampler,
                batch_size=args.eval_batch_size,
            )

            # multi-gpu eval
            if args.n_gpu > 1 and not isinstance(model, torch.nn.DataParallel):
                model = torch.nn.DataParallel(model)

            # Eval!
            logger.info("***** Running evaluation {} *****".format(prefix))
            logger.info("  Num examples = %d", len(eval_dataset))
            logger.info("  Batch size = %d", args.eval_batch_size)
            eval_loss = 0.0
            nb_eval_steps = 0
            preds = None
            out_label_ids = None
            for batch in tqdm(eval_dataloader, desc="Evaluating"):

                model.num_labels = args.num_label_list[
                    args.task_names.index(eval_task.replace("-mm", ""))
                ]
                model.eval()
                batch = tuple(t.to(args.device) for t in batch)

                with torch.no_grad():
                    inputs = {
                        "input_ids": batch[0],
                        "attention_mask": batch[1],
                        "labels": batch[3],
                    }
                    if args.model_type != "distilbert":
                        inputs["token_type_ids"] = (
                            batch[2]
                            if args.model_type in ["bert", "xlnet", "albert"]
                            else None
                        )  # XLM, DistilBERT, RoBERTa, and XLM-RoBERTa don't use segment_ids
                    outputs = model(**inputs)
                    tmp_eval_loss, logits = outputs[:2]

                    eval_loss += tmp_eval_loss.mean().item()
                nb_eval_steps += 1
                if preds is None:
                    preds = logits.detach().cpu().numpy()
                    out_label_ids = inputs["labels"].detach().cpu().numpy()
                else:
                    preds = np.append(
                        preds, logits.detach().cpu().numpy(), axis=0
                    )
                    out_label_ids = np.append(
                        out_label_ids,
                        inputs["labels"].detach().cpu().numpy(),
                        axis=0,
                    )

            eval_loss = eval_loss / nb_eval_steps
            if output_mode == "classification":
                preds = np.argmax(preds, axis=1)
            elif output_mode == "regression":
                preds = np.squeeze(preds)
            result = compute_metrics(eval_task, preds, out_label_ids)
            results.update(result)

            output_eval_file = os.path.join(
                eval_output_dir, prefix, "eval_results.txt"
            )
            print("Eval results to", output_eval_file)
            if not os.path.isdir(os.path.join(eval_output_dir, prefix)):
                os.makedirs(os.path.join(eval_output_dir, prefix))
            with open(output_eval_file, "w") as writer:
                logger.info("***** Eval results {} *****".format(prefix))
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))
                    writer.write("%s = %s\n" % (key, str(result[key])))

        full_results[eval_task] = results
    return full_results


def load_and_cache_examples(
    args, task, data_dir, tokenizer, evaluate=False, shorten=False
):
    print("Loading task {} and data_dir {}".format(task, data_dir))
    if args.local_rank not in [-1, 0] and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    print("Loading task", task)
    try:
        processor = processors[task]()
        output_mode = output_modes[task]
        dir_task = task
    except Exception:
        processor = processors[task[:-1]]()
        output_mode = output_modes[task[:-1]]
    # Load data features from cache or dataset file
    name_to_use = (
        args.model_type
        if "/" in args.model_name_or_path
        else args.model_name_or_path
    )
    cached_features_file = os.path.join(
        data_dir,
        "cached_{}_{}_{}_{}".format(
            "dev" if evaluate else "train",
            list(filter(None, args.model_name_or_path.split("/"))).pop(),
            str(args.max_seq_length),
            str(task),
        ),
    )
    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info(
            "Loading features from cached file %s", cached_features_file
        )
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", data_dir)
        label_list = processor.get_labels()
        print(label_list, "is the label list")
        if task in ["mnli", "mnli-mm"] and args.model_type in [
            "roberta",
            "xlmroberta",
        ]:
            # HACK(label indices are swapped in RoBERTa pretrained model)
            label_list[1], label_list[2] = label_list[2], label_list[1]
        examples = (
            processor.get_dev_examples(data_dir)
            if evaluate
            else processor.get_train_examples(data_dir)
        )
        try:
            features = convert_examples_to_features(
                examples[:2] if shorten else examples,
                tokenizer,
                label_list=label_list,
                max_length=args.max_seq_length,
                output_mode=output_mode,
                pad_on_left=bool(
                    args.model_type in ["xlnet"]
                ),  # pad on the left for xlnet
                pad_token=tokenizer.convert_tokens_to_ids(
                    [tokenizer.pad_token]
                )[0],
                pad_token_segment_id=4 if args.model_type in ["xlnet"] else 0,
            )
        except Exception as e:
            print(e)
            raise (e)

        if args.local_rank in [-1, 0]:
            logger.info(
                "Saving features into cached file %s", cached_features_file
            )
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor(
        [f.input_ids for f in features], dtype=torch.long
    )
    all_attention_mask = torch.tensor(
        [f.attention_mask for f in features], dtype=torch.long
    )
    all_token_type_ids = torch.tensor(
        [f.token_type_ids for f in features], dtype=torch.long
    )
    if output_mode == "classification":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.long)
    elif output_mode == "regression":
        all_labels = torch.tensor(
            [f.label for f in features], dtype=torch.float
        )

    dataset = TensorDataset(
        all_input_ids, all_attention_mask, all_token_type_ids, all_labels
    )
    return dataset


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--data_dirs",
        default=None,
        type=str,
        required=True,
        help="The input data dirs seperate by a space. Should contain the .tsv files (or other data files) for the task.",
    )
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: "
        + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: "
        + ", ".join(ALL_MODELS),
    )
    parser.add_argument(
        "--task_names",
        default=None,
        type=str,
        required=True,
        help="The name of the tasks (seperated by spaces) to train selected in the list: "
        + ", ".join(processors.keys()),
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )

    # Other parameters
    parser.add_argument(
        "--config_name",
        default="",
        type=str,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--single_task_scores",
        default="",
        type=str,
        help="The single task scores of each task seperated by a space",
    )
    parser.add_argument(
        "--tokenizer_name",
        default="",
        type=str,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--cache_dir",
        default="",
        type=str,
        help="Where do you want to store the pre-trained models downloaded from s3",
    )
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument(
        "--do_train", action="store_true", help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval",
        action="store_true",
        help="Whether to run eval on the dev set.",
    )
    parser.add_argument(
        "--evaluate_during_training",
        action="store_true",
        help="Run evaluation during training at each logging step.",
    )
    parser.add_argument(
        "--do_lower_case",
        action="store_true",
        help="Set this flag if you are using an uncased model.",
    )

    parser.add_argument(
        "--per_gpu_train_batch_size",
        default=8,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--per_gpu_eval_batch_size",
        default=8,
        type=int,
        help="Batch size per GPU/CPU for evaluation.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam.",
    )
    parser.add_argument(
        "--weight_decay",
        default=0.0,
        type=float,
        help="Weight decay if we apply some.",
    )
    parser.add_argument(
        "--adam_epsilon",
        default=1e-8,
        type=float,
        help="Epsilon for Adam optimizer.",
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=3.0,
        type=float,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--warmup_steps",
        default=0,
        type=int,
        help="Linear warmup over warmup_steps.",
    )

    parser.add_argument(
        "--logging_steps",
        type=int,
        default=100,
        help="Log every X updates steps.",
    )
    parser.add_argument(
        "--save_steps",
        type=int,
        default=100,
        help="Save checkpoint every X updates steps.",
    )
    parser.add_argument(
        "--save_checkpoints",
        type=int,
        default=10,
        help="Save X checkpoints from all epochs",
    )
    parser.add_argument(
        "--eval_all_checkpoints",
        action="store_true",
        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number",
    )
    parser.add_argument(
        "--no_cuda", action="store_true", help="Avoid using CUDA when available"
    )
    parser.add_argument(
        "--overwrite_output_dir",
        action="store_true",
        help="Overwrite the content of the output directory",
    )
    parser.add_argument(
        "--overwrite_cache",
        action="store_true",
        help="Overwrite the cached training and evaluation sets",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for initialization"
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
        "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )
    parser.add_argument(
        "--server_ip", type=str, default="", help="For distant debugging."
    )
    parser.add_argument(
        "--server_port", type=str, default="", help="For distant debugging."
    )
    parser.add_argument(
        "--sampling_type",
        type=str,
        default="uniform",
        help="The type of multi-task sampling to do in ['uniform', 'size' or 'dynamic'].",
    )
    parser.add_argument(
        "--batch_type",
        type=str,
        default="heterogeneous",
        help="The type of loss updating to do in ['heterogeneous', 'forced_heterogeneous', 'homogeneous', 'partition']",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        default=False,
        help="Useful for debugging on small data (use True)",
    )
    parser.add_argument(
        "--start_again",
        action="store_true",
        default=False,
        help="For ignoring global step when loading a model",
    )
    args = parser.parse_args()

    if (
        os.path.exists(args.output_dir)
        and os.listdir(args.output_dir)
        and args.do_train
        and not args.overwrite_output_dir
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir
            )
        )

    if os.path.isfile(os.path.join(args.output_dir, "results.csv")):
        print(
            "Already finished executing file",
            os.path.join(args.output_dir, "results.csv"),
        )
        exit(0)

    if args.short:
        args.per_gpu_train_batch_size = 2

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        print("Waiting for debugger attach")
        ptvsd.enable_attach(
            address=(args.server_ip, args.server_port), redirect_output=True
        )
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu"
        )
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device
    print(f"Using device {device}, ")
    set_seed(args)  # Added here for reproductibility

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.fp16,
    )

    # Prepare GLUE task
    processor_list = []
    label_lists = []
    num_label_list = []
    args.task_names = args.task_names.lower().split(" ")
    for task_name in args.task_names:
        if task_name not in processors:
            raise ValueError("Task not found: %s" % (task_name))
        cur_processor = processors[task_name]()
        cur_label_list = cur_processor.get_labels()
        processor_list.append(cur_processor)
        label_lists.append(cur_label_list)
        num_label_list.append(len(cur_label_list))

    args.num_label_list = num_label_list

    # create needed task/data_dirs
    data_dirs = args.data_dirs.split(" ")
    print(f"Data dirs are {data_dirs}")
    args.data_dir_list = data_dirs
    args.task_list = [item.lower() for item in args.task_names]
    print("Working with tasks {}".format(args.task_list))
    print("Working with data_dirs", args.data_dir_list)

    if args.sampling_type == "dynamic":
        single_scores = pd.read_csv(
            args.single_task_scores, header=0, index_col=0
        )
        args.single_task_scores = [
            single_scores.loc[dir_name.split("/")[-1].split("_")[0]][0]
            for dir_name in data_dirs
        ]

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(
        args.config_name if args.config_name else args.model_name_or_path,
        num_labels=num_label_list[0],
        finetuning_task=args.task_names[0],
        cache_dir=args.cache_dir if args.cache_dir else None,
    )
    tokenizer = tokenizer_class.from_pretrained(
        args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
        do_lower_case=args.do_lower_case,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )
    model = model_class.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)

    # Training
    if args.do_train:
        train_datasets = []
        for index, task_name in enumerate(args.task_names):
            train_datasets.append(
                load_and_cache_examples(
                    args,
                    args.task_list[index],
                    args.data_dir_list[index],
                    tokenizer,
                    evaluate=False,
                    shorten=args.short,
                )
            )
        args.train_batch_size = args.per_gpu_train_batch_size * max(
            1, args.n_gpu
        )
        train_dataset = MTLDataset(
            args, train_datasets, args.train_batch_size, args.single_task_scores
        )
        global_step, tr_loss = train(args, train_dataset, model, tokenizer)
        logger.info(
            " global_step = %s, average loss = %s", global_step, tr_loss
        )

    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.do_train and (
        args.local_rank == -1 or torch.distributed.get_rank() == 0
    ):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = (
            model.module if hasattr(model, "module") else model
        )  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, "training_args.bin"))

        # Load a trained model and vocabulary that you have fine-tuned
        model = model_class.from_pretrained(args.output_dir)
        tokenizer = tokenizer_class.from_pretrained(args.output_dir)
        model.to(args.device)

    # Evaluation
    results = {}
    if args.do_eval and args.local_rank in [-1, 0]:
        tokenizer = tokenizer_class.from_pretrained(
            args.output_dir, do_lower_case=args.do_lower_case
        )
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(
                os.path.dirname(c)
                for c in sorted(
                    glob.glob(
                        args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True
                    )
                )
            )
            logging.getLogger("transformers.modeling_utils").setLevel(
                logging.WARN
            )  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = (
                checkpoint.split("-")[-1] if len(checkpoints) > 1 else ""
            )
            prefix = (
                checkpoint.split("/")[-1]
                if checkpoint.find("checkpoint") != -1
                else ""
            )
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            result = evaluate(args, model, tokenizer, prefix=prefix)
            result = dict(
                (k + "_{}".format(global_step), v) for k, v in result.items()
            )
            results.update(result)

    print(
        "Writing `results.json` to file at {}".format(
            os.path.join(args.output_dir, "results.json")
        )
    )
    with open(os.path.join(args.output_dir, "results.json"), "w") as fout:
        fout.write(json.dumps(results))
    return results


if __name__ == "__main__":
    main()
