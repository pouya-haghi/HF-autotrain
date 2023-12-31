import argparse
import json
import os
import sys
from functools import partial

import pandas as pd
import torch
import torch.nn as nn
from accelerate import Accelerator
from accelerate.state import PartialState
from datasets import Dataset, load_dataset
from huggingface_hub import HfApi
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
    default_data_collator,
)
from trl import SFTTrainer

from autotrain import logger
from autotrain.trainers.clm import utils
from autotrain.trainers.clm.callbacks import LoadBestPeftModelCallback, SavePeftModelCallback
from autotrain.trainers.clm.params import LLMTrainingParams
from autotrain.utils import monitor


def parse_args():
    # get training_config.json from the end user
    parser = argparse.ArgumentParser()
    parser.add_argument("--training_config", type=str, required=True)
    return parser.parse_args()


@monitor
def train(config):
    # print("from trainer clm")
    if isinstance(config, dict):
        config = LLMTrainingParams(**config)

    if config.repo_id is None and config.username is not None:
        config.repo_id = f"{config.username}/{config.project_name}"

    # TODO: remove when SFT is fixed
    # if config.trainer == "sft":
    #     config.trainer = "default"

    # check if config.train_split.csv exists in config.data_path
    if config.train_split is not None:
        train_path = f"{config.data_path}/{config.train_split}.csv"
        if os.path.exists(train_path):
            logger.info("loading dataset from csv")
            train_data = pd.read_csv(train_path)
            train_data = Dataset.from_pandas(train_data)
        else:
            train_data = load_dataset(
                config.data_path,
                split=config.train_split,
                token=config.token,
            )

    if config.valid_split is not None:
        valid_path = f"{config.data_path}/{config.valid_split}.csv"
        if os.path.exists(valid_path):
            logger.info("loading dataset from csv")
            valid_data = pd.read_csv(valid_path)
            valid_data = Dataset.from_pandas(valid_data)
        else:
            valid_data = load_dataset(
                config.data_path,
                split=config.valid_split,
                token=config.token,
            )

    tokenizer = AutoTokenizer.from_pretrained(
        config.model,
        token=config.token,
        trust_remote_code=True,
    )

    if tokenizer.model_max_length > 2048:
        tokenizer.model_max_length = config.model_max_length

    if getattr(tokenizer, "pad_token", None) is None:
        tokenizer.pad_token = tokenizer.eos_token

    if config.trainer == "default":
        train_data = utils.process_data(
            data=train_data,
            tokenizer=tokenizer,
            config=config,
        )
        if config.valid_split is not None:
            valid_data = utils.process_data(
                data=valid_data,
                tokenizer=tokenizer,
                config=config,
            )

    model_config = AutoConfig.from_pretrained(
        config.model,
        token=config.token,
        trust_remote_code=True,
    )

    if config.use_peft:
        if config.use_int4:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=config.use_int4,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=False,
            )
        elif config.use_int8:
            bnb_config = BitsAndBytesConfig(load_in_8bit=config.use_int8)
        else:
            bnb_config = BitsAndBytesConfig()

        model = AutoModelForCausalLM.from_pretrained(
            config.model,
            config=model_config,
            token=config.token,
            quantization_config=bnb_config,
            torch_dtype=torch.float16,
            device_map={"": Accelerator().process_index} if torch.cuda.is_available() else None,
            trust_remote_code=True,
            use_flash_attention_2=config.use_flash_attention_2,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            config.model,
            config=model_config,
            token=config.token,
            trust_remote_code=True,
            use_flash_attention_2=config.use_flash_attention_2,
        )

    model.resize_token_embeddings(len(tokenizer))

    if config.use_peft:
        if config.use_int8 or config.use_int4:
            model = prepare_model_for_kbit_training(model)
        peft_config = LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=utils.get_target_modules(config),
        )
        model = get_peft_model(model, peft_config)

    if config.block_size == -1:
        config.block_size = None

    if config.block_size is None:
        block_size = tokenizer.model_max_length
        if block_size > 1024:
            logger.warning(
                "The chosen tokenizer supports a `model_max_length` that is longer than the default `block_size` value"
                " of 1024. If you would like to use a longer `block_size` up to `tokenizer.model_max_length` you can"
                " override this default with `--block_size xxx`."
            )
            block_size = 1024
    else:
        if config.block_size > tokenizer.model_max_length:
            logger.warning(
                f"The block_size passed ({config.block_size}) is larger than the maximum length for the model"
                f"({tokenizer.model_max_length}). Using block_size={tokenizer.model_max_length}."
            )
        block_size = min(config.block_size, tokenizer.model_max_length)

    config.block_size = block_size

    if config.trainer == "default":
        tokenize_fn = partial(utils.tokenize, tokenizer=tokenizer, config=config)
        group_texts_fn = partial(utils.group_texts, config=config)

        train_data = train_data.map(
            tokenize_fn,
            batched=True,
            num_proc=1,
            remove_columns=list(train_data.features),
            desc="Running tokenizer on train dataset",
        )

        if config.valid_split is not None:
            valid_data = valid_data.map(
                tokenize_fn,
                batched=True,
                num_proc=1,
                remove_columns=list(valid_data.features),
                desc="Running tokenizer on validation dataset",
            )

        train_data = train_data.map(
            group_texts_fn,
            batched=True,
            num_proc=4,
            desc=f"Grouping texts in chunks of {block_size}",
        )

        if config.valid_split is not None:
            valid_data = valid_data.map(
                group_texts_fn,
                batched=True,
                num_proc=4,
                desc=f"Grouping texts in chunks of {block_size}",
            )

    # PH: start (float8)
    # # convert to float8 (and any custom float)
    # # DONT FORGET TO FIRST QUANTIZE WEIGHTS TO LNS16
    # # IN YOUR HOOK, YOU HAVE TO USE .CLONE OF THE ARGUMNETS AND THEN MODIFY IT AND FINALLY RETURN IT. IT DOESNT WORK WITHOUT CLONE (IT HAS TO BE OUT OF PLACE COMPUTATION, NOT IN-PLACE)

    # # Create a 32-bit float tensor 3 bit mantissa, 4 bit exponent
    # num_bit_exponent = 4
    # num_bit_mantissa  = 3
    # offset = torch.tensor(2**(num_bit_exponent-1))
    # scale = torch.tensor(2 ** num_bit_mantissa)
    # threshold_clamp = 2**(num_bit_exponent-1)
    # threshold_up = float(2**threshold_clamp)
    # threshold_down = float(2**-(threshold_clamp))

    # # float32_tensor = torch.tensor(3.14159, dtype=torch.float32)

    # # # Extract sign, exponent, and mantissa bits from the 32-bit float
    # # # sign_bit = float32_tensor.sign()
    # # exponent_bits = torch.floor(torch.log2(torch.abs(float32_tensor))) + offset
    # # exponent = torch.pow(2, (exponent_bits - offset))
    # # mantissa_bits = torch.round(((float32_tensor / exponent) - 1) * scale)
    # # apx_float = ((mantissa_bits/scale) + 1) * exponent

    # # For keeping track of activations:
    # # class ReferenceCounter:
    # #     def __init__(self):
    # #         self.count = 0
    # #     def increase(self):
    # #         self.count += 1
    # #     def get_count(self):
    # #         return self.count

    # # counter = ReferenceCounter()
    # # list_output_activation = {}

    # class STEFunction_structured(torch.autograd.Function):
    #     """ define straight through estimator with overrided gradient (gate) """
        # @staticmethod
        # def forward(ctx, input):
        #     # ctx.save_for_backward(input.clone()) # if you want to use input during backward calculation
        #     # output = input.clone()
        #     if isinstance(input, tuple):
        #         # Clone each tensor in the tuple
        #         output = tuple(t.clone() for t in input)
        #         output = tuple(torch.where(t < 0, -torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up), torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up)) for t in output)
        #         output = tuple(((torch.round(((t / torch.pow(2, (torch.floor(torch.log2(torch.abs(t)))))) - 1) * scale)/scale) + 1) * torch.pow(2, (torch.floor(torch.log2(torch.abs(t))))) for t in output)
        #         return output                
        #     else:
        #         # If input is not a tuple, clone it
        #         output = input.clone()
        #         # handling overflow/underflow (b/c of limited # of bits for mantissa) -> sparsify if less than a threshold and report an error message if larger thana threshold
        #         clamped_output = torch.clamp(torch.abs(output), min=threshold_down, max=threshold_up)
        #         output = torch.where(output<0, -clamped_output, clamped_output)

        #         exponent_bits = torch.floor(torch.log2(torch.abs(output))) + offset
        #         exponent = torch.pow(2, (exponent_bits - offset))
        #         mantissa_bits = torch.round(((output / exponent) - 1) * scale)
        #         output = ((mantissa_bits/scale) + 1) * exponent
    #             return output

        # @staticmethod
        # def backward(ctx, grad_output):
        #     # # aux1 = ctx.saved_tensors # if you want to use input during backward calculation
        #     grad_input = grad_output.clone()
        #     return grad_input
        #     # # aux1 = ctx.saved_tensors # if you want to use input during backward calculation
        #     # handling overflow/underflow (b/c of limited # of bits for mantissa) -> sparsify if less than a threshold and report an error message if larger thana threshold
        #     # grad_input = grad_output.clone()
        #     # clamped_output = torch.clamp(torch.abs(grad_input), min=threshold_down, max=threshold_up)
        #     # grad_input = torch.where(grad_input<0, -clamped_output, clamped_output)

        #     # exponent_bits = torch.floor(torch.log2(torch.abs(grad_input))) + offset
        #     # exponent = torch.pow(2, (exponent_bits - offset))
        #     # mantissa_bits = torch.round(((grad_input / exponent) - 1) * scale)
        #     # grad_input = ((mantissa_bits/scale) + 1) * exponent
        #     # return grad_input

    # def activation_hook(module, input, output):
    #     output = STEFunction_structured.apply(output)
    #     # for keeping track of activations
    #     # list_output_activation[str(module.__class__.__name__)+str("_")+str(counter.get_count())] = output #$$$
    #     # counter.increase() #$$$
    #     return output

    # EXCLUDED_ACTIVATIONS = (nn.ReLU, nn.Tanh, nn.GELU, nn.Sigmoid, nn.Softmax, nn.LeakyReLU, nn.PReLU)

    # for name, module in model.named_modules():
    #     if not isinstance(module, nn.ModuleList) and not list(module.children()) and "intermediate_act_fn" not in name and not isinstance(module, nn.LayerNorm) and not isinstance(module, nn.Dropout) and not any(isinstance(module, activation) for activation in EXCLUDED_ACTIVATIONS):
    #         module.register_forward_hook(activation_hook)
    # PH: end

    # # PH: start (LNS8)
    # # CONVERT TO LNS16/LNS8
    # # DONT FORGET TO FIRST QUANTIZE WEIGHTS TO LNS16
    # # IN YOUR HOOK, YOU HAVE TO USE .CLONE OF THE ARGUMNETS AND THEN MODIFY IT AND FINALLY RETURN IT. IT DOESNT WORK WITHOUT CLONE (IT HAS TO BE OUT OF PLACE COMPUTATION, NOT IN-PLACE)
    # #old version:
    # # num_bit_mantissa = 5 # for 16 bit repr.
    # # # num_bit_mantissa = 3 # for 8 bit repr.
    # # num_frac = 10 # fractional bits for 16 bit repr.
    # # # num_frac = 4 # fractional bits for 8 bit repr.

    # # new version:
    # # num_bit_mantissa = 5 # for 16 bit repr.
    # num_bit_mantissa = 4 # for 8 bit repr.
    # # num_frac = 10 # fractional bits for 16 bit repr.
    # num_frac = 3 # fractional bits for 8 bit repr.
    # scale = float(2**num_frac)
    # threshold_clamp = 2**(num_bit_mantissa-1)
    # threshold_up = float(2**threshold_clamp)
    # threshold_down = float(2**-(threshold_clamp))

    # # For keeping track of activations:
    # # class ReferenceCounter:
    # #     def __init__(self):
    # #         self.count = 0
    # #     def increase(self):
    # #         self.count += 1
    # #     def get_count(self):
    # #         return self.count

    # # counter = ReferenceCounter()
    # # list_output_activation = {}

    # class STEFunction_structured(torch.autograd.Function):
    #     """ define straight through estimator with overrided gradient (gate) """
    #     @staticmethod
    #     def forward(ctx, input):
    #         # ctx.save_for_backward(input.clone()) # if you want to use input during backward calculation
    #         if isinstance(input, tuple):
    #             # Clone each tensor in the tuple
    #             output = tuple(t.clone() for t in input)
    #             output = tuple(torch.where(t < 0, -torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up), torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up)) for t in output)
    #             output = tuple(torch.where(t > 0, torch.pow(2,(torch.round(torch.log2(t)*scale))/scale), torch.where(t < 0, -torch.pow(2,(torch.round(torch.log2(-t)*scale)/scale)), t)) for t in output)
    #             return output                
    #         else:
    #             output = input.clone()
    #             # handling overflow/underflow (b/c of limited # of bits for mantissa) -> sparsify if less than a threshold and report an error message if larger thana threshold
    #             clamped_output = torch.clamp(torch.abs(output), min=threshold_down, max=threshold_up)
    #             output = torch.where(output<0, -clamped_output, clamped_output)
    #             # v1: concise
    #             output = torch.where(output > 0, torch.pow(2,(torch.round(torch.log2(output)*scale))/scale), torch.where(output < 0, -torch.pow(2,(torch.round(torch.log2(-output)*scale)/scale)), output))
    #             return output

    #     @staticmethod
    #     def backward(ctx, grad_output):
    #         # aux1 = ctx.saved_tensors # if you want to use input during backward calculation
    #         grad_input = grad_output.clone()
    #         return grad_input
    #         # uncomment the follwoing line to also quantize gradients during bakward prop.
    #         # grad_input = grad_output.clone()
    #         # # handling overflow/underflow (b/c of limited # of bits for mantissa) -> sparsify if less than a threshold and report an error message if larger thana threshold
    #         # clamped_output = torch.clamp(torch.abs(grad_input), min=threshold_down, max=threshold_up)
    #         # grad_input = torch.where(grad_input<0, -clamped_output, clamped_output)
    #         # # v1: concise
    #         # grad_input = torch.where(grad_input > 0, torch.pow(2,(torch.round(torch.log2(grad_input)*scale))/scale), torch.where(grad_input < 0, -torch.pow(2,(torch.round(torch.log2(-grad_input)*scale)/scale)), grad_input))
    #         # return grad_input

    # def activation_hook(module, input, output):
    #     output = STEFunction_structured.apply(output)
    #     # for keeping track of activations
    #     # list_output_activation[str(module.__class__.__name__)+str("_")+str(counter.get_count())] = output #$$$
    #     # counter.increase() #$$$
    #     return output

    # EXCLUDED_ACTIVATIONS = (nn.ReLU, nn.Tanh, nn.GELU, nn.Sigmoid, nn.Softmax, nn.LeakyReLU, nn.PReLU)

    # for name, module in model.named_modules():
    #     if not isinstance(module, nn.ModuleList) and not list(module.children()) and "intermediate_act_fn" not in name and not isinstance(module, nn.LayerNorm) and not isinstance(module, nn.Dropout) and not any(isinstance(module, activation) for activation in EXCLUDED_ACTIVATIONS):
    #         module.register_forward_hook(activation_hook)
    #         # print(name)
    # # PH: end

    # PH: start (modified LNS8)
    # num_bit_mantissa = 5 # for 16 bit repr.
    num_bit_mantissa = 5 # for 8 bit repr.
    threshold_mantissa = 2**(num_bit_mantissa-1)
    threshold_up = float(2**threshold_mantissa)
    threshold_down = float(2**-(threshold_mantissa))

    # new version:
    max_num_bit_mantissa_needed = 1 # according to the distribution you can get this number
    log_domain_threshold = 2** max_num_bit_mantissa_needed # 4
    real_domain_threshold_up = float(2**log_domain_threshold) # 16
    real_domain_threshold_down = float(2**(-log_domain_threshold)) # 1/16

    # num_frac_low_prec = 10 # number of fractional bits for 16 bit repr.
    num_frac_low_prec = 2 # number of fractional bits for 8 bit repr.
    # num_frac_high_prec = num_frac_low_prec + (num_bit_mantissa-max_num_bit_mantissa_needed) # 13
    num_frac_high_prec = num_frac_low_prec + 2 # 13
    scale_low_prec = 2**(num_frac_low_prec)
    scale_high_prec = 2**(num_frac_high_prec)
    # v3:
    num_frac_highest_prec = num_frac_high_prec + 2 # for extreme outliers
    scale_highest_prec = 2**(num_frac_highest_prec)

    # For keeping track of activations:
    # class ReferenceCounter:
    #     def __init__(self):
    #         self.count = 0
    #     def increase(self):
    #         self.count += 1
    #     def get_count(self):
    #         return self.count

    # counter = ReferenceCounter()
    # list_output_activation = {}

    class STEFunction_structured(torch.autograd.Function):
        """ define straight through estimator with overrided gradient (gate) """
        @staticmethod
        def forward(ctx, input):
            # ctx.save_for_backward(input.clone()) # if you want to use input during backward calculation
            if isinstance(input, tuple):
                output = tuple(t.clone() for t in input)
                output = tuple(torch.where(t<0, -torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up), torch.clamp(torch.abs(t), min=threshold_down, max=threshold_up)) for t in output)
                output = tuple(torch.where(t > 0, torch.pow(2, torch.where(torch.log2(t)>torch.max(torch.log2(t))-5, torch.where(torch.log2(t)>torch.max(torch.log2(t))-3, torch.round(torch.log2(t) * scale_highest_prec)/ scale_highest_prec, torch.round(torch.log2(t) * scale_high_prec)/ scale_high_prec), torch.round(torch.log2(t) * scale_low_prec)/ scale_low_prec)), torch.where(t < 0, -torch.pow(2, torch.where(torch.log2(-t)>torch.max(torch.log2(-t))-5, torch.where(torch.log2(-t)>torch.max(torch.log2(-t))-3, torch.round(torch.log2(-t) * scale_highest_prec)/ scale_highest_prec, torch.round(torch.log2(-t) * scale_high_prec)/ scale_high_prec), torch.round(torch.log2(-t) * scale_low_prec)/ scale_low_prec)), t)) for t in output)
                return output
            else:
                output = input.clone()
                # handling overflow/underflow (b/c of limited # of bits for mantissa) -> sparsify if less than a threshold and report an error message if larger thana threshold
                clamped_output = torch.clamp(torch.abs(output), min=threshold_down, max=threshold_up)
                output = torch.where(output<0, -clamped_output, clamped_output)

                # v3:
                if len(output.shape) == 3: # 3D
                  non_zero_indices = output.nonzero()
                  non_zero_values = output[non_zero_indices[:, 0], non_zero_indices[:, 1], non_zero_indices[:, 2]] # 0 because the first dimension is batch, 1 b/c next one is first dimension of feature, 2 b/c it is second dimension of features
                  # if 1D: non_zero_indices
                  if len(non_zero_values) > 0: # any nonzero avail
                    log_x = torch.where(non_zero_values > 0, torch.log2(non_zero_values), torch.log2(-non_zero_values))
                    quant_exponent_low_prec = torch.round(log_x * scale_low_prec)/ scale_low_prec # 2**3 - round(+ 0.5)
                    quant_exponent_high_prec = torch.round(log_x * scale_high_prec)/ scale_high_prec # 2**3 - round(+ 0.5)
                    # --------  v3 (including extreme outliers) ---------
                    quant_exponent_highest_prec = torch.round(log_x * scale_highest_prec)/ scale_highest_prec # 2**3 - round(+ 0.5)
                    max_val = torch.max(log_x)
                    quant_exponent = torch.where(log_x>max_val-5, torch.where(log_x>max_val-3, quant_exponent_highest_prec, quant_exponent_high_prec), quant_exponent_low_prec) # max_val-3 and max_val-5 are thresholds for extreme and moderate outliers (beta nd gamma)
                    # ------- end v3 ---------
                    quantized_values = torch.where(non_zero_values > 0, torch.pow(2, quant_exponent), -(torch.pow(2, quant_exponent)))
                    output[non_zero_indices[:, 0], non_zero_indices[:, 1], non_zero_indices[:, 2]] = quantized_values
                elif len(output.shape) == 2: # 2D
                  non_zero_indices = output.nonzero()
                  non_zero_values = output[non_zero_indices[:, 0], non_zero_indices[:, 1]] # 0 because the first dimension is batch, 1 b/c next one is first dimension of feature, 2 b/c it is second dimension of features
                  if len(non_zero_values) > 0:
                    log_x = torch.where(non_zero_values > 0, torch.log2(non_zero_values), torch.log2(-non_zero_values))
                    quant_exponent_low_prec = torch.round(log_x * scale_low_prec) / scale_low_prec # 2**3 - round(+ 0.5)
                    quant_exponent_high_prec = torch.round(log_x * scale_high_prec) / scale_high_prec # 2**3 - round(+ 0.5)
                    # --------  v3 (including extreme outliers) ---------
                    quant_exponent_highest_prec = torch.round(log_x * scale_highest_prec)/ scale_highest_prec # 2**3 - round(+ 0.5)
                    max_val = torch.max(log_x)
                    quant_exponent = torch.where(log_x>max_val-5, torch.where(log_x>max_val-3, quant_exponent_highest_prec, quant_exponent_high_prec), quant_exponent_low_prec) # max_val-3 and max_val-5 are thresholds for extreme and moderate outliers (beta nd gamma)
                    # ------- end v3 ---------
                    quantized_values = torch.where(non_zero_values > 0, torch.pow(2, quant_exponent), -(torch.pow(2, quant_exponent)))
                    output[non_zero_indices[:, 0], non_zero_indices[:, 1]] = quantized_values
                else:
                  print("Out of shape")
                return output

        @staticmethod
        def backward(ctx, grad_output):
            # aux1 = ctx.saved_tensors # if you want to use input during backward calculation
            grad_input = grad_output.clone()
            return grad_input

    def activation_hook(module, input, output):
        output = STEFunction_structured.apply(output)
        return output

    EXCLUDED_ACTIVATIONS = (nn.ReLU, nn.Tanh, nn.GELU, nn.Sigmoid, nn.Softmax, nn.LeakyReLU, nn.PReLU)

    for name, module in model.named_modules():
        if not isinstance(module, nn.ModuleList) and not list(module.children()) and "intermediate_act_fn" not in name and not isinstance(module, nn.LayerNorm) and not isinstance(module, nn.Dropout) and not any(isinstance(module, activation) for activation in EXCLUDED_ACTIVATIONS):
            module.register_forward_hook(activation_hook)

    # PH: end

    logger.info("creating trainer")
    # trainer specific
    if config.logging_steps == -1:
        if config.valid_split is not None:
            logging_steps = int(0.2 * len(valid_data) / config.batch_size)
        else:
            logging_steps = int(0.2 * len(train_data) / config.batch_size)
        if logging_steps == 0:
            logging_steps = 1

    else:
        logging_steps = config.logging_steps

    training_args = dict(
        output_dir=config.project_name,
        per_device_train_batch_size=config.batch_size,
        per_device_eval_batch_size=config.batch_size,
        learning_rate=config.lr,
        num_train_epochs=config.epochs,
        evaluation_strategy=config.evaluation_strategy if config.valid_split is not None else "no",
        logging_steps=logging_steps,
        save_total_limit=config.save_total_limit,
        save_strategy=config.save_strategy,
        gradient_accumulation_steps=config.gradient_accumulation,
        report_to="tensorboard",
        auto_find_batch_size=config.auto_find_batch_size,
        lr_scheduler_type=config.scheduler,
        optim=config.optimizer,
        warmup_ratio=config.warmup_ratio,
        weight_decay=config.weight_decay,
        max_grad_norm=config.max_grad_norm,
        fp16=config.fp16,
        push_to_hub=False,
        load_best_model_at_end=True if config.valid_split is not None else False,
        ddp_find_unused_parameters=False,
    )

    args = TrainingArguments(**training_args)

    callbacks = []
    if config.use_peft:
        callbacks.append(SavePeftModelCallback)
        if config.valid_split is not None:
            callbacks.append(LoadBestPeftModelCallback)

    trainer_args = dict(
        args=args,
        model=model,
    )

    if config.trainer == "default":
        trainer = Trainer(
            **trainer_args,
            train_dataset=train_data,
            eval_dataset=valid_data if config.valid_split is not None else None,
            tokenizer=tokenizer,
            data_collator=default_data_collator,
            callbacks=callbacks,
        )
    elif config.trainer == "sft":
        trainer = SFTTrainer(
            **trainer_args,
            train_dataset=train_data,
            eval_dataset=valid_data if config.valid_split is not None else None,
            peft_config=peft_config if config.use_peft else None,
            dataset_text_field=config.text_column,
            max_seq_length=config.block_size,
            tokenizer=tokenizer,
            packing=True,
        )
    else:
        raise ValueError(f"trainer `{config.trainer}` not supported")
    model.config.use_cache = False

    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    for name, module in trainer.model.named_modules():
        # if isinstance(module, LoraLayer):
        #     if script_args.bf16:
        #         module = module.to(torch.bfloat16)
        if "norm" in name:
            module = module.to(torch.float32)
        # if "lm_head" in name or "embed_tokens" in name:
        #     if hasattr(module, "weight"):
        #         if script_args.bf16 and module.weight.dtype == torch.float32:
        #             module = module.to(torch.bfloat16)

    trainer.train()

    logger.info("Finished training, saving model...")
    trainer.save_model(config.project_name)

    model_card = utils.create_model_card()

    # save model card to output directory as README.md
    with open(f"{config.project_name}/README.md", "w") as f:
        f.write(model_card)

    if config.use_peft and config.merge_adapter:
        logger.info("Merging adapter weights...")
        try:
            utils.merge_adapter(
                base_model_path=config.model,
                target_model_path=config.project_name,
                adapter_path=config.project_name,
            )
        except Exception as e:
            logger.warning(f"Failed to merge adapter weights: {e}")
            logger.warning("Skipping adapter merge. Only adapter weights will be saved.")

    if config.push_to_hub:
        if PartialState().process_index == 0:
            logger.info("Pushing model to hub...")
            if os.path.exists(f"{config.project_name}/training_params.json"):
                training_params = json.load(open(f"{config.project_name}/training_params.json"))
                training_params.pop("token")
                json.dump(training_params, open(f"{config.project_name}/training_params.json", "w"))
            api = HfApi(token=config.token)
            api.create_repo(repo_id=config.repo_id, repo_type="model", private=True)
            api.upload_folder(folder_path=config.project_name, repo_id=config.repo_id, repo_type="model")

    if PartialState().process_index == 0:
        if "SPACE_ID" in os.environ:
            # shut down the space
            logger.info("Pausing space...")
            api = HfApi(token=config.token)
            api.pause_space(repo_id=os.environ["SPACE_ID"])

        if "ENDPOINT_ID" in os.environ:
            # shut down the endpoint
            logger.info("Pausing endpoint...")
            utils.pause_endpoint(config)


if __name__ == "__main__":
    args = parse_args()
    training_config = json.load(open(args.training_config))
    config = LLMTrainingParams(**training_config)
    train(config)
