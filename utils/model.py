import re
import time
import random
from pathlib import Path
from functools import lru_cache
from threading import Thread, Lock
from dataclasses import dataclass
from pprint import pformat, pprint

from pydantic import BaseModel

from tqdm import tqdm
from argparse import ArgumentParser
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
from sampler_hijack import hijack_samplers

from typing import *

import utils
from utils import consts, log_generation_config

import logging

logger = logging.getLogger(__name__)

@dataclass
class SakuraModelConfig:
    # read from console
    model_name_or_path: str
    use_gptq_model: bool
    trust_remote_code: bool = False
    llama: bool = False
    text_length: int = 512

    # read from config.json (model_name_or_path)
    model_name: str|None = None
    model_quant: str|None = None
    model_version: str = "0.8"  # Can be also read from terminal, double check this.

def load_model(args: SakuraModelConfig):
    if args.use_gptq_model:
        from auto_gptq import AutoGPTQForCausalLM

    if args.llama:
        from transformers import LlamaForCausalLM, LlamaTokenizer

    if args.trust_remote_code is False and args.model_version in "0.5 0.7 0.8":
        raise ValueError("If you use model version 0.5, 0.7 or 0.8, please add flag --trust_remote_code.")

    logger.info("loading model ...")

    if args.llama:
        tokenizer = LlamaTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=args.trust_remote_code)
    else:
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=False, trust_remote_code=args.trust_remote_code, use_safetensors=False)

    if args.use_gptq_model:
        model = AutoGPTQForCausalLM.from_quantized(args.model_name_or_path, device="cuda:0", trust_remote_code=args.trust_remote_code, use_safetensors=False)
    else:
        if args.llama:
            model = LlamaForCausalLM.from_pretrained(args.model_name_or_path, device_map="auto", trust_remote_code=args.trust_remote_code)
        else:
            model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, device_map="auto", trust_remote_code=args.trust_remote_code, use_safetensors=False)

    return (tokenizer, model)


class SakuraModel:
    # typing
    class ModelResponse(BaseModel):
        context_token: int
        new_token: int
        text: str


    def __init__(
        self,
        cfg: SakuraModelConfig,
    ):
        self.cfg = cfg

        hijack_samplers()

        # Global lock for model generation.
        self.lock = Lock()

        (tokenizer, model) = load_model(cfg)
        self.tokenizer = tokenizer
        self.model = model


        try:
            model_name = self.model.config.sakura_name
            model_version = self.model.config.sakura_version
            model_quant = self.model.config.sakura_quant
        except:
            logger.error("Some attrs are missing in model config.json (sakura_{name|version|quant}), please check the model!")
            exit(-1)

        # FIXME(kuriko): Currently we only know quant version from terminal, we cannot depend on parsing `model_name_or_path`
        # FIXME(kuriko): sakura_xxx is hard coded here, maybe we can find a better way.
        if (input_model_version := self.cfg.model_version) != model_version:
            logger.error(f"Model version check failed, {input_model_version} != {model_version}")
            logger.error(f"Current config: {model_name}-v{model_version}-{model_quant}")
            exit(-1)

        self.cfg.model_name = model_name
        self.cfg.model_version = model_version
        self.cfg.model_quant = model_quant

        return

    def get_cfg(self) -> SakuraModelConfig:
        return self.cfg

    def check_model_by_magic(self) -> bool:
        (prompt, ground_truth, output) = self.test_loaded()
        logger.debug(f"test output: {output}")
        logger.debug(f"ground truth: {ground_truth}")
        ret = ground_truth == output.text
        if not ret:
            logging.warning(f"model output is not correct, please check the loaded model")
            logging.warning(f"input: {prompt}")
            logging.warning(f"ground_truth: {ground_truth}")
            logging.warning(f"current output: {output}")
        return ret


    def get_max_text_length(self, length: int) -> int:
        return max(self.cfg.text_length, length)

    def completion(self, prompt: str, generation_config: GenerationConfig, is_print_speed:bool=True) -> ModelResponse:
        log_generation_config(generation_config)

        output = self.get_model_response(
            self.model,
            self.tokenizer,
            prompt,
            self.cfg.model_version,
            generation_config,
            self.get_max_text_length(len(prompt)),
            is_print_speed,
        )

        return output

    def test_loaded(self) -> (str, ModelResponse):
        testcase = consts.get_test_case_by_model_version(self.cfg.model_version)

        generation_config = testcase.generation_config
        test_input = testcase.test_input
        test_output = testcase.test_output

        prompt = consts.get_prompt(test_input, self.cfg.model_version)
        output = self.completion(prompt, generation_config, is_print_speed=False)

        return prompt, test_output, output


    def get_model_response(self, model: AutoModelForCausalLM, tokenizer: AutoTokenizer, prompt: str, model_version: str, generation_config: GenerationConfig, text_length: int, is_print_speed:bool=True) -> ModelResponse:
        with self.lock:  # using lock to prevent too many memory allocated on GPU
            t0 = time.time()
            input_token = tokenizer(prompt, return_tensors="pt")
            input_token_len = input_token.input_ids.shape[-1]
            generation = model.generate(**input_token.to(model.device), generation_config=generation_config)[0]
            new_token = generation.shape[-1] - input_token_len
            response = tokenizer.decode(generation)
            output = utils.split_response(response, model_version)
            t1 = time.time()

        if is_print_speed:
            logger.info(f'Output generated in {(t1-t0):.2f} seconds ({new_token/(t1-t0):.2f} tokens/s, {new_token} tokens, context {input_token_len} tokens)')

        return self.ModelResponse(
            context_token = input_token_len,
            new_token = new_token,
            text = output,
        )

    def get_model_response_anti_degen(self, model: AutoModelForCausalLM, tokenizer: AutoTokenizer, prompt: str, model_version: str, generation_config: GenerationConfig, text_length: int):
        backup_generation_config_stage2 = GenerationConfig(
                temperature=0.1,
                top_p=0.3,
                top_k=40,
                num_beams=1,
                bos_token_id=1,
                eos_token_id=2,
                pad_token_id=0,
                max_new_tokens=2 * text_length,
                min_new_tokens=1,
                do_sample=True,
                repetition_penalty=1.0,
                frequency_penalty=0.05
            )

        backup_generation_config_stage3 = GenerationConfig(
                temperature=0.1,
                top_p=0.3,
                top_k=40,
                num_beams=1,
                bos_token_id=1,
                eos_token_id=2,
                pad_token_id=0,
                max_new_tokens=2 * text_length,
                min_new_tokens=1,
                do_sample=True,
                repetition_penalty=1.0,
                frequency_penalty=0.2
            )

        backup_generation_config = [backup_generation_config_stage2, backup_generation_config_stage3]

        generation = model.generate(**tokenizer(prompt, return_tensors="pt").to(model.device), generation_config=generation_config)[0]
        if len(generation) > 2 * text_length:
            stage = 0
            while utils.detect_degeneration(list(generation), model_version):
                stage += 1
                if stage > 2:
                    print("model degeneration cannot be avoided.")
                    break
                generation = model.generate(**tokenizer(prompt, return_tensors="pt").to(model.device), generation_config=backup_generation_config[stage-1])[0]
        response = tokenizer.decode(generation)
        output = utils.split_response(response, model_version)
        return output
