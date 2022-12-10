import argparse
import itertools
import math
import os
from contextlib import nullcontext
from pathlib import Path
from typing import Optional
import subprocess
import sys
import shutil

import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch.utils.data import Dataset

from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from diffusers import AutoencoderKL, PNDMScheduler, StableDiffusionPipeline, UNet2DConditionModel
from diffusers.optimization import get_scheduler
from huggingface_hub import HfFolder, Repository, whoami
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer

import json

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--subfolder_mode",
        action="store_true",
        help="Whether to train on images in subfolders in the format of ./class_prompt/group_name/text_prompt (1).jpg",
    )
    parser.add_argument(
        "--save_intermediary_dirs",
        default=0,
        type=int,
        help="Flag to save intermediary dirs.",
    )
    parser.add_argument(
        "--diffusers_to_ckpt_script_path",
        type=str,
        default="/content/diffusers/scripts/convert_diffusers_to_original_stable_diffusion.py",
        required=True,
        help="Path to the script to convert diffusers model to SD ckpt file.",
    )
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--instance_data_dir",
        type=str,
        default=None,
        required=True,
        help="A folder containing the training data of instance images.",
    )
    parser.add_argument(
        "--class_data_dir",
        type=str,
        default=None,
        required=False,
        help="A folder containing the training data of class images.",
    )
    parser.add_argument(
        "--instance_prompt",
        type=str,
        default=None,
        help="The prompt with identifier specifying the instance",
    )
    parser.add_argument(
        "--class_prompt",
        type=str,
        default=None,
        help="The prompt to specify images in the same class as provided instance images.",
    )
    parser.add_argument(
        "--with_prior_preservation",
        default=False,
        action="store_true",
        help="Flag to add prior preservation loss.",
    )
    parser.add_argument("--prior_loss_weight", type=float, default=1.0, help="The weight of prior preservation loss.")
    parser.add_argument(
        "--num_class_images",
        type=int,
        default=100,
        help=(
            "Minimal class images for prior preservation loss. If not have enough images, additional images will be"
            " sampled with class_prompt."
        ),
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--center_crop", action="store_true", help="Whether to center crop images before resizing to resolution"
    )
    parser.add_argument("--train_text_encoder", action="store_true", help="Whether to train the text encoder")
    parser.add_argument(
        "--train_batch_size", type=int, default=4, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument(
        "--sample_batch_size", type=int, default=4, help="Batch size (per device) for sampling images."
    )
    parser.add_argument("--num_train_epochs", type=int, default=1)
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-6,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument(
        "--use_8bit_adam", action="store_true", help="Whether or not to use 8-bit Adam from bitsandbytes."
    )
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument("--hub_token", type=str, default=None, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--hub_model_id",
        type=str,
        default=None,
        help="The name of the repository to keep in sync with the local `output_dir`.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument("--not_cache_latents", action="store_true", help="Do not precompute and cache latents from VAE.")
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default="no",
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose"
            "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
            "and an Nvidia Ampere GPU."
        ),
    )

    parser.add_argument(
        "--save_n_steps",
        type=int,
        default=1,
        help=("Save the model every n global_steps"),
    )

    parser.add_argument(
        "--save_starting_step",
        type=int,
        default=1,
        help=("The step from which it starts saving intermediary checkpoints"),
    )

    parser.add_argument(
        "--stop_text_encoder_training",
        type=int,
        default=1000000,
        help=("The step at which the text_encoder is no longer trained"),
    )

    parser.add_argument(
        "--image_captions_filename",
        action="store_true",
        help="Get captions from filename",
    )

    parser.add_argument(
        "--Session_dir",
        type=str,
        default="",
        help="Current session directory",
    )

    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")

    args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    if args.instance_data_dir is None:
        raise ValueError("You must specify a train data directory.")

    if args.with_prior_preservation:
        if args.class_data_dir is None:
            raise ValueError("You must specify a data directory for class images.")
        if args.class_prompt is None and not args.subfolder_mode:
            raise ValueError("You must specify prompt for class images.")

    return args


class ClassDataProvider:
    """
    Provides class image paths in round-robin.
    """

    def __init__(
        self,
        class_data_root: str,
        class_prompt: str
    ):
        self.class_prompt = class_prompt
        class_data_dir = Path(os.path.join(class_data_root, class_prompt))
        class_data_dir.mkdir(parents=True, exist_ok=True)
        self.class_images = [x for x in class_data_dir.iterdir() if not x.is_dir()]
        self.cursor = 0
        if len(self.class_images) <= 0:
            raise ValueError(f"Empty class directory: {class_data_dir}")

    def take_one(self) -> Path:
        r = self.class_images[self.cursor]
        self.cursor = (self.cursor + 1) % len(self.class_images)
        return r


class SubfolderModeDataset(Dataset):
    """
    A dataset to prepare the instance and class images (in subfolder mode) with the prompts for fine-tuning the model.
    It pre-processes the images and the tokenizes prompts.
    """

    def __init__(
        self,
        instance_data_root,
        tokenizer,
        args,
        class_data_root,
        size=512,
        center_crop=False,
    ):
        self.size = size
        self.center_crop = center_crop
        self.tokenizer = tokenizer

        if not Path(instance_data_root).exists():
            raise ValueError("Instance images root doesn't exists.")

        # Get all the nested classes and instance images
        class_subdirs = [x for x in Path(instance_data_root).iterdir() if x.is_dir()]
        if len(class_subdirs) <= 0:
            raise ValueError("Instance image root directory does not have any class subfolders.")
        elif len(class_subdirs) > 3:
            raise ValueError(f"Class subfolders ({len(class_subdirs)}) exceed memory limit.")
        self.classes = {}
        self.instance_images_path = []
        for class_subdir in class_subdirs:
            self.classes[class_subdir.name] = ClassDataProvider(class_data_root=class_data_root,
                                                                class_prompt=class_subdir.name)

            # Commented out as directory structure is strictly ./class_prompt/group_name/instance_prompt (x).jpg
            # images_path = [x for x in class_subdir.iterdir() if not x.is_dir()]
            # self.instance_images_path.extend(images_path)

            instance_images_dirs = [x for x in class_subdir.iterdir() if x.is_dir()]
            if len(instance_images_dirs) > 0:
                if len(instance_images_dirs) > 5:
                    raise ValueError(f"Instance image directories ({len(instance_images_dirs)}) exceed memory limit.")
                for instance_images_dir in instance_images_dirs:
                    images_path = [x for x in instance_images_dir.iterdir() if not x.is_dir()]
                    self.instance_images_path.extend(images_path)

        self.num_instance_images = len(self.instance_images_path)
        self._length = self.num_instance_images

        print(f"Initializing SubfolderModeDataset, {len(class_subdirs)} classes, {self.num_instance_images} instance images...")
        sys.stdout.flush()

        self.image_transforms = transforms.Compose(
            [
                transforms.Resize(size, interpolation=transforms.InterpolationMode.BILINEAR),
                transforms.CenterCrop(size) if center_crop else transforms.RandomCrop(size),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
        )

    def __len__(self):
        return self._length

    def __getitem__(self, index):
        example = {}
        path = self.instance_images_path[index % self.num_instance_images]
        instance_image = Image.open(path)
        if not instance_image.mode == "RGB":
            instance_image = instance_image.convert("RGB")

        # Get instant prompt from file name
        filename = Path(path).stem
        pt = ''.join([i for i in filename if not i.isdigit()])
        pt = pt.replace("_", " ")
        pt = pt.replace("(", "")
        pt = pt.replace(")", "")
        pt = pt.replace("-", "")
        pt = pt.replace(",,", ",").replace(",,", ",").replace(",,", ",")
        instance_prompt = pt
        sys.stdout.write(" [0;32m" + instance_prompt + " [0m")
        sys.stdout.flush()

        # Get instance images
        example["instance_images"] = self.image_transforms(instance_image)
        example["instance_prompt_ids"] = self.tokenizer(
            instance_prompt,
            padding="do_not_pad",
            truncation=True,
            max_length=self.tokenizer.model_max_length,
        ).input_ids

        # Get class prompt from instance image path
        class_prompt = path.parent.parent.name
        class_data = self.classes[class_prompt]
        if class_data is None:
            raise ValueError("Class data does not exist for class prompt: " + class_prompt)

        # Get class images for the instance image
        class_image = Image.open(class_data.take_one())
        if not class_image.mode == "RGB":
            class_image = class_image.convert("RGB")
        example["class_images"] = self.image_transforms(class_image)
        example["class_prompt_ids"] = self.tokenizer(
            class_prompt,
            padding="do_not_pad",
            truncation=True,
            max_length=self.tokenizer.model_max_length,
        ).input_ids

        return example


class DreamBoothDataset(Dataset):
    """
    A dataset to prepare the instance and class images with the prompts for fine-tuning the model.
    It pre-processes the images and the tokenizes prompts.
    """

    def __init__(
        self,
        instance_data_root,
        instance_prompt,
        tokenizer,
        args,
        class_data_root=None,
        class_prompt=None,
        size=512,
        center_crop=False,
    ):
        self.size = size
        self.center_crop = center_crop
        self.tokenizer = tokenizer
        self.image_captions_filename = None

        self.instance_data_root = Path(instance_data_root)
        if not self.instance_data_root.exists():
            raise ValueError("Instance images root doesn't exists.")

        self.instance_images_path = list(Path(instance_data_root).iterdir())
        self.num_instance_images = len(self.instance_images_path)
        self.instance_prompt = instance_prompt
        self._length = self.num_instance_images

        if args.image_captions_filename:
            self.image_captions_filename = True

        if class_data_root is not None:
            self.class_data_root = Path(class_data_root)
            self.class_data_root.mkdir(parents=True, exist_ok=True)
            self.class_images_path = list(self.class_data_root.iterdir())
            self.num_class_images = len(self.class_images_path)
            self._length = max(self.num_class_images, self.num_instance_images)
            self.class_prompt = class_prompt
        else:
            self.class_data_root = None

        self.image_transforms = transforms.Compose(
            [
                transforms.Resize(size, interpolation=transforms.InterpolationMode.BILINEAR),
                transforms.CenterCrop(size) if center_crop else transforms.RandomCrop(size),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
        )

    def __len__(self):
        return self._length

    def __getitem__(self, index):
        example = {}
        path = self.instance_images_path[index % self.num_instance_images]
        instance_image = Image.open(path)
        if not instance_image.mode == "RGB":
            instance_image = instance_image.convert("RGB")

        instance_prompt = self.instance_prompt

        if self.image_captions_filename:
            filename = Path(path).stem
            pt = ''.join([i for i in filename if not i.isdigit()])
            pt = pt.replace("_", " ")
            pt = pt.replace("(", "")
            pt = pt.replace(")", "")
            pt = pt.replace("-", "")
            instance_prompt = pt
            sys.stdout.write(" [0;32m" + instance_prompt + " [0m")
            sys.stdout.flush()

        example["instance_images"] = self.image_transforms(instance_image)
        example["instance_prompt_ids"] = self.tokenizer(
            instance_prompt,
            padding="do_not_pad",
            truncation=True,
            max_length=self.tokenizer.model_max_length,
        ).input_ids

        if self.class_data_root:
            class_image = Image.open(self.class_images_path[index % self.num_class_images])
            if not class_image.mode == "RGB":
                class_image = class_image.convert("RGB")
            example["class_images"] = self.image_transforms(class_image)
            example["class_prompt_ids"] = self.tokenizer(
                self.class_prompt,
                padding="do_not_pad",
                truncation=True,
                max_length=self.tokenizer.model_max_length,
            ).input_ids

        return example


class PromptDataset(Dataset):
    "A simple dataset to prepare the prompts to generate class images on multiple GPUs."

    def __init__(self, prompt, num_samples):
        self.prompt = prompt
        self.num_samples = num_samples

    def __len__(self):
        return self.num_samples

    def __getitem__(self, index):
        example = {}
        example["prompt"] = self.prompt
        example["index"] = index
        return example


class LatentsDataset(Dataset):
    def __init__(self, latents_cache, text_encoder_cache):
        self.latents_cache = latents_cache
        self.text_encoder_cache = text_encoder_cache

    def __len__(self):
        return len(self.latents_cache)

    def __getitem__(self, index):
        return self.latents_cache[index], self.text_encoder_cache[index]


class AverageMeter:
    def __init__(self, name=None):
        self.name = name
        self.reset()

    def reset(self):
        self.sum = self.count = self.avg = 0

    def update(self, val, n=1):
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def get_full_repo_name(model_id: str, organization: Optional[str] = None, token: Optional[str] = None):
    if token is None:
        token = HfFolder.get_token()
    if organization is None:
        username = whoami(token)["name"]
        return f"{username}/{model_id}"
    else:
        return f"{organization}/{model_id}"


def generate_class_samples(args, accelerator: Accelerator, class_images_dir: Path, class_prompt: str):
    cur_class_images = len(list(class_images_dir.iterdir()))
    num_new_images = args.num_class_images - cur_class_images
    if num_new_images <= 0:
        return

    torch_dtype = torch.float16 if accelerator.device.type == "cuda" else torch.float32
    pipeline = StableDiffusionPipeline.from_pretrained(
        args.pretrained_model_name_or_path, torch_dtype=torch_dtype
    )
    pipeline.set_progress_bar_config(disable=True)

    logger.info(f"Number of class images to sample: {num_new_images}.")

    sample_dataset = PromptDataset(class_prompt, num_new_images)
    sample_dataloader = torch.utils.data.DataLoader(sample_dataset, batch_size=args.sample_batch_size)

    sample_dataloader = accelerator.prepare(sample_dataloader)
    pipeline.to(accelerator.device)

    for example in tqdm(
        sample_dataloader, desc="Generating class images for '{class_prompt}'", disable=not accelerator.is_local_main_process
    ):
        with torch.autocast("cuda"), torch.inference_mode():
            images = pipeline(example["prompt"]).images

        for i, image in enumerate(images):
            image.save(class_images_dir / f"{example['index'][i] + cur_class_images}.jpg")

    del pipeline
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def main():
    # Clear cache
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    args = parse_args()
    logging_dir = Path(args.output_dir, args.logging_dir)
    i = args.save_starting_step

    if args.seed is None or args.seed == 0:
        args.seed = 1337
    set_seed(args.seed)

    if args.subfolder_mode:
        args.image_captions_filename = True
        args.with_prior_preservation = True
        args.prior_loss_weight = 1.0

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with="tensorboard",
        logging_dir=logging_dir,
    )

    if args.stop_text_encoder_training is None or args.stop_text_encoder_training <= 1:
        args.train_text_encoder = False

    # Currently, it's not possible to do gradient accumulation when training two models with accelerate.accumulate
    # This will be enabled soon in accelerate. For now, we don't allow gradient accumulation when training two models.
    # TODO (patil-suraj): Remove this check when gradient accumulation with two models is enabled in accelerate.
    if args.train_text_encoder and args.gradient_accumulation_steps > 1 and accelerator.num_processes > 1:
        raise ValueError(
            "Gradient accumulation is not supported when training the text encoder in distributed training. "
            "Please set gradient_accumulation_steps to 1. This feature will be supported in the future."
        )

    # Generate class images if needed.
    if args.with_prior_preservation:
        if not args.subfolder_mode:
            class_images_dir = Path(args.class_data_dir)
            class_images_dir.mkdir(parents=True, exist_ok=True)
            generate_class_samples(args, accelerator=accelerator, class_images_dir=class_images_dir, class_prompt=args.class_prompt)
        else:
            instance_images_dir = Path(args.instance_data_dir)
            instance_images_dir.mkdir(parents=True, exist_ok=True)
            class_subdirs = [x for x in instance_images_dir.iterdir() if x.is_dir()]
            for class_subdir in class_subdirs:
                class_images_dir = Path(os.path.join(args.class_data_dir, class_subdir.name))
                class_images_dir.mkdir(parents=True, exist_ok=True)
                generate_class_samples(args, accelerator=accelerator, class_images_dir=class_images_dir, class_prompt=class_subdir.name)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.push_to_hub:
            if args.hub_model_id is None:
                repo_name = get_full_repo_name(Path(args.output_dir).name, token=args.hub_token)
            else:
                repo_name = args.hub_model_id
            repo = Repository(args.output_dir, clone_from=repo_name)

            with open(os.path.join(args.output_dir, ".gitignore"), "w+") as gitignore:
                if "step_*" not in gitignore:
                    gitignore.write("step_*\n")
                if "epoch_*" not in gitignore:
                    gitignore.write("epoch_*\n")
        elif args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # Load the tokenizer
    if args.tokenizer_name:
        tokenizer = CLIPTokenizer.from_pretrained(args.tokenizer_name)
    elif args.pretrained_model_name_or_path:
        tokenizer = CLIPTokenizer.from_pretrained(args.pretrained_model_name_or_path, subfolder="tokenizer")

    # Load models and create wrapper for stable diffusion
    text_encoder = CLIPTextModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="text_encoder")
    vae = AutoencoderKL.from_pretrained(args.pretrained_model_name_or_path, subfolder="vae")
    unet = UNet2DConditionModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="unet")

    vae.requires_grad_(False)
    if not args.train_text_encoder:
        text_encoder.requires_grad_(False)

    if args.gradient_checkpointing:
        unet.enable_gradient_checkpointing()
        if args.train_text_encoder:
            text_encoder.gradient_checkpointing_enable()

    print("learning_rate: " + str(args.learning_rate))
    print("num_processes: " + str(accelerator.num_processes))
    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
        )
    print("learning_rate (after scaled): " + str(args.learning_rate))

    # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
    if args.use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    else:
        optimizer_class = torch.optim.AdamW

    params_to_optimize = (
        itertools.chain(unet.parameters(), text_encoder.parameters()) if args.train_text_encoder else unet.parameters()
    )
    optimizer = optimizer_class(
        params_to_optimize,
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    noise_scheduler = PNDMScheduler.from_config(args.pretrained_model_name_or_path, subfolder="scheduler")

    if not args.subfolder_mode:
        train_dataset = DreamBoothDataset(
            instance_data_root=args.instance_data_dir,
            instance_prompt=args.instance_prompt,
            class_data_root=args.class_data_dir if args.with_prior_preservation else None,
            class_prompt=args.class_prompt,
            tokenizer=tokenizer,
            size=args.resolution,
            center_crop=args.center_crop,
            args=args,
        )
    else:
        train_dataset = SubfolderModeDataset(
            instance_data_root=args.instance_data_dir,
            tokenizer=tokenizer,
            args=args,
            class_data_root=args.class_data_dir,
            size=args.resolution,
            center_crop=args.center_crop,
        )

    def collate_fn(examples):
        input_ids = [example["instance_prompt_ids"] for example in examples]
        pixel_values = [example["instance_images"] for example in examples]

        # Concat class and instance examples for prior preservation.
        # We do this to avoid doing two forward passes.
        if args.with_prior_preservation:
            input_ids += [example["class_prompt_ids"] for example in examples]
            pixel_values += [example["class_images"] for example in examples]

        pixel_values = torch.stack(pixel_values)
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

        input_ids = tokenizer.pad({"input_ids": input_ids}, padding=True, return_tensors="pt").input_ids

        batch = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
        }
        return batch

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.train_batch_size, shuffle=True, collate_fn=collate_fn, pin_memory=True
    )

    weight_dtype = torch.float32
    if args.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif args.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move text_encode and vae to gpu.
    # For mixed precision training we cast the text_encoder and vae weights to half-precision
    # as these models are only used for inference, keeping weights in full precision is not required.
    vae.to(accelerator.device, dtype=weight_dtype)
    if not args.train_text_encoder:
        text_encoder.to(accelerator.device, dtype=weight_dtype)

    if not args.not_cache_latents:
        latents_cache = []
        text_encoder_cache = []
        for batch in tqdm(train_dataloader, desc="Caching latents"):
            with torch.no_grad():
                batch["pixel_values"] = batch["pixel_values"].to(accelerator.device, non_blocking=True, dtype=weight_dtype)
                batch["input_ids"] = batch["input_ids"].to(accelerator.device, non_blocking=True)
                latents_cache.append(vae.encode(batch["pixel_values"]).latent_dist)
                if args.train_text_encoder:
                    text_encoder_cache.append(batch["input_ids"])
                else:
                    text_encoder_cache.append(text_encoder(batch["input_ids"])[0])
        train_dataset = LatentsDataset(latents_cache, text_encoder_cache)
        train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=1, collate_fn=lambda x: x, shuffle=True)

        del vae
        if not args.train_text_encoder:
            del text_encoder
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
    )

    if args.train_text_encoder:
        unet, text_encoder, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, text_encoder, optimizer, train_dataloader, lr_scheduler
        )
    else:
        unet, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, optimizer, train_dataloader, lr_scheduler
        )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers("dreambooth", config=vars(args))

    def bar(prg):
        br = '|' + '█' * prg + ' ' * (25 - prg) + '|'
        return br

    # Train!
    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num batches each epoch = {len(train_dataloader)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process, total=len(range(0, args.max_train_steps, 1)))
    global_step = 0

    # Load last session if exists
    froze_text = False
    session = {"session_step": 0}
    sessionFilePath = args.output_dir + '/training/session.json'
    if not os.path.isdir(args.output_dir + '/training'):
        os.makedirs(args.output_dir + '/training')
    if os.path.isfile(sessionFilePath) and os.path.getsize(sessionFilePath) > 0 and os.path.getsize(sessionFilePath) < 10000:
        # with open(sessionFilePath, "rb") as f:
        #     session = pickle.load(f)
        with open(sessionFilePath, "r") as f:
            session = json.load(f)

    loss_avg = AverageMeter()
    text_enc_context = nullcontext() if args.train_text_encoder else torch.no_grad()
    for epoch in range(args.num_train_epochs):
        unet.train()
        if args.train_text_encoder:
            text_encoder.train()
        for step, batch in enumerate(train_dataloader):
            # set_seed(args.seed + 10000 * (global_step % 100000))

            with accelerator.accumulate(unet):
                # Convert images to latent space
                with torch.no_grad():
                    if not args.not_cache_latents:
                        latent_dist = batch[0][0]
                    else:
                        latent_dist = vae.encode(batch["pixel_values"].to(dtype=weight_dtype)).latent_dist
                    latents = latent_dist.sample() * 0.18215

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
                timesteps = timesteps.long()

                # Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                # Get the text embedding for conditioning
                with text_enc_context:
                    if not args.not_cache_latents:
                        if args.train_text_encoder:
                            encoder_hidden_states = text_encoder(batch[0][1])[0]
                        else:
                            encoder_hidden_states = batch[0][1]
                    else:
                        encoder_hidden_states = text_encoder(batch["input_ids"])[0]

                # Predict the noise residual
                noise_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample

                if args.with_prior_preservation:
                    # Chunk the noise and noise_pred into two parts and compute the loss on each part separately.
                    noise_pred, noise_pred_prior = torch.chunk(noise_pred, 2, dim=0)
                    noise, noise_prior = torch.chunk(noise, 2, dim=0)

                    # Compute instance loss
                    loss = F.mse_loss(noise_pred.float(), noise.float(), reduction="none").mean([1, 2, 3]).mean()

                    # Compute prior loss
                    prior_loss = F.mse_loss(noise_pred_prior.float(), noise_prior.float(), reduction="mean")

                    # Add the prior loss to the instance loss.
                    loss = loss + args.prior_loss_weight * prior_loss
                else:
                    loss = F.mse_loss(noise_pred.float(), noise.float(), reduction="mean")

                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    params_to_clip = (
                        itertools.chain(unet.parameters(), text_encoder.parameters())
                        if args.train_text_encoder
                        else unet.parameters()
                    )
                    accelerator.clip_grad_norm_(params_to_clip, args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1

            fll = round((global_step * 100) / args.max_train_steps)
            fll = round(fll / 4)
            pr = bar(fll)

            logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)
            progress_bar.set_description_str("Progress:" + pr)
            accelerator.log(logs, step=global_step)

            if global_step >= args.max_train_steps:
                break

            if args.train_text_encoder and global_step == args.stop_text_encoder_training and global_step >= 30:
                if accelerator.is_main_process:
                    print(" [0;32m" + " Freezing the text_encoder ..." + " [0m")
                    frz_dir = args.output_dir + "/text_encoder_frozen"
                    if os.path.isdir(frz_dir):
                        #subprocess.call('rm -r '+ frz_dir, shell=True)
                        shutil.rmtree(frz_dir)
                    os.mkdir(frz_dir)
                    pipeline = StableDiffusionPipeline.from_pretrained(
                        args.pretrained_model_name_or_path,
                        unet=accelerator.unwrap_model(unet),
                        text_encoder=accelerator.unwrap_model(text_encoder),
                    )
                    pipeline.text_encoder.save_pretrained(frz_dir)
                    del pipeline
                else:
                    print("DID NOT freeze text encoder as not in the main process!")

            if args.save_n_steps >= 200:
                if global_step + 1 < args.max_train_steps and global_step + 1 == i:
                    ckpt_name = "_" + str(session["session_step"] + global_step + 1)
                    save_dir = Path(args.output_dir + ckpt_name)
                    save_dir = str(save_dir)
                    save_dir = save_dir.replace(" ", "_")
                    if not os.path.exists(save_dir):
                        os.mkdir(save_dir)
                    inst = os.path.basename(os.path.dirname(args.output_dir + '/')) + ckpt_name
                    inst = inst.replace(" ", "_")
                    print(" [1;32mSAVING CHECKPOINT: " + args.Session_dir + "/" + inst + ".ckpt")
                    # Create the pipeline using the trained modules and save it.
                    if accelerator.is_main_process:
                        pipeline = StableDiffusionPipeline.from_pretrained(
                            args.pretrained_model_name_or_path,
                            unet=accelerator.unwrap_model(unet),
                            text_encoder=accelerator.unwrap_model(text_encoder),
                        )
                        pipeline.save_pretrained(save_dir)
                        del pipeline
                        frz_dir = args.output_dir + "/text_encoder_frozen"
                        if args.train_text_encoder and os.path.exists(frz_dir):
                            #subprocess.call('rm -r '+save_dir+'/text_encoder/*.*', shell=True)
                            #subprocess.call('cp -f '+frz_dir +'/*.* '+ save_dir+'/text_encoder', shell=True)
                            shutil.rmtree(save_dir + '/text_encoder')
                            shutil.copytree(frz_dir, save_dir + '/text_encoder', dirs_exist_ok=True)
                        chkpth = args.Session_dir + "/" + inst + ".ckpt"
                        subprocess.call('python3 ' + args.diffusers_to_ckpt_script_path + ' --model_path ' + save_dir + ' --checkpoint_path ' + chkpth + ' --half', shell=True)
                        i = i + args.save_n_steps

                        if args.save_intermediary_dirs == 0:
                            #subprocess.call('rm -rf '+ save_dir, shell=True)
                            shutil.rmtree(save_dir)

            if accelerator.sync_gradients and accelerator.is_main_process and global_step % 20 == 0:
                print("")
                sys.stdout.flush()

        accelerator.wait_for_everyone()

    # Create the pipeline using using the trained modules and save it.
    if accelerator.is_main_process:
        pipeline = StableDiffusionPipeline.from_pretrained(
            args.pretrained_model_name_or_path,
            unet=accelerator.unwrap_model(unet),
            text_encoder=accelerator.unwrap_model(text_encoder),
        )
        frz_dir = args.output_dir + "/text_encoder_frozen"
        pipeline.save_pretrained(args.output_dir)
        del pipeline
        if args.train_text_encoder and os.path.exists(frz_dir):
            #subprocess.call('mv -f '+frz_dir +'/*.* '+ args.output_dir+'/text_encoder', shell=True)
            #subprocess.call('rm -r '+ frz_dir, shell=True)
            if os.path.isdir(args.output_dir + '/text_encoder'):
                shutil.rmtree(args.output_dir + '/text_encoder')
            shutil.copytree(frz_dir, args.output_dir + '/text_encoder', dirs_exist_ok=True)
            shutil.rmtree(frz_dir)

        if args.push_to_hub:
            repo.push_to_hub(commit_message="End of training", blocking=False, auto_lfs_prune=True)

    accelerator.end_training()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # Save state for resuming
    session["session_step"] += args.max_train_steps
    if not os.path.isdir(args.output_dir + '/training'):
        os.makedirs(args.output_dir + '/training')
    # with open(sessionFilePath, "wb+") as f:
    #     pickle.dump(session, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(sessionFilePath, "w+") as f:
        json.dump(session, f)

    # Save final ckpt
    if os.path.isfile(args.output_dir + '/unet/diffusion_pytorch_model.bin'):
        final_chkpth = args.output_dir + '/' + os.path.basename(os.path.dirname(args.output_dir + '/')) + '_' + str(session["session_step"]) + '.ckpt'
        print("Saving the ckpt model...")
        if os.path.isfile(final_chkpth):
            os.remove(final_chkpth)
        subprocess.call('python3 ' + args.diffusers_to_ckpt_script_path + ' --model_path ' + args.output_dir + ' --checkpoint_path ' + final_chkpth + ' --half', shell=True)
        if os.path.isfile(final_chkpth):
            print("Saved model to " + final_chkpth)
        else:
            print("Failed to save model to " + final_chkpth)
        sys.stdout.flush()
    else:
        print('No model to save!')


if __name__ == "__main__":
    main()
