MODEL = 'stabilityai/stable-diffusion-xl-base-1.0'
VARIANT = None
CUSTOM_PIPELINE = None
VAE = 'madebyollin/sdxl-vae-fp16-fix'
SCHEDULER = 'EulerAncestralDiscreteScheduler'
LORA = None
CONTROLNET = 'diffusers/controlnet-depth-sdxl-1.0'
STEPS = 20
PROMPT = 'best quality, realistic, unreal engine, 4K, a beautiful girl'
NEGATIVE_PROMPT = None
SEED = None
WARMUPS = 3
BATCH = 1
HEIGHT = 1024
WIDTH = 1024
INPUT_IMAGE = None
CONTROL_IMAGE = None
OUTPUT_IMAGE = None
EXTRA_CALL_KWARGS = None
QUANTIZE: bool | None = True

import os
import importlib
import inspect
import argparse
import time
import json
import torch
from argparse import Namespace
from typing import Any
from PIL import (Image, ImageDraw)
from diffusers.utils import load_image
from sfast.compilers.diffusion_pipeline_compiler import (compile,
                                                         CompilationConfig)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default=MODEL)
    parser.add_argument('--variant', type=str, default=VARIANT)
    parser.add_argument('--custom-pipeline', type=str, default=CUSTOM_PIPELINE)
    parser.add_argument('--scheduler', type=str, default=SCHEDULER)
    parser.add_argument('--vae', type=str, default=VAE)
    parser.add_argument('--lora', type=str, default=LORA)
    parser.add_argument('--controlnet', type=str, default=CONTROLNET)
    parser.add_argument('--steps', type=int, default=STEPS)
    parser.add_argument('--prompt', type=str, default=PROMPT)
    parser.add_argument('--negative-prompt', type=str, default=NEGATIVE_PROMPT)
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--warmups', type=int, default=WARMUPS)
    parser.add_argument('--batch', type=int, default=BATCH)
    parser.add_argument('--height', type=int, default=HEIGHT)
    parser.add_argument('--width', type=int, default=WIDTH)
    parser.add_argument('--extra-call-kwargs',
                        type=str,
                        default=EXTRA_CALL_KWARGS)
    parser.add_argument('--input-image', type=str, default=INPUT_IMAGE)
    parser.add_argument('--control-image', type=str, default=CONTROL_IMAGE)
    parser.add_argument('--output-image', type=str, default=OUTPUT_IMAGE)
    parser.add_argument(
        '--compiler',
        type=str,
        default='sfast',
        choices=['none', 'sfast', 'compile', 'compile-max-autotune'])
    parser.add_argument('--quantize', action='store_true', default=QUANTIZE)
    parser.add_argument('--no-fusion', action='store_true')
    parser.add_argument('--print-image-terminal', action='store_true')
    return parser.parse_args()


def load_model(pipeline_cls,
               model,
               variant=None,
               custom_pipeline=None,
               scheduler=None,
               lora=None,
               controlnet=None,
               vae=None,
):
    extra_kwargs = {}
    if custom_pipeline is not None:
        extra_kwargs['custom_pipeline'] = custom_pipeline
    if variant is not None:
        extra_kwargs['variant'] = variant
    if controlnet is not None:
        from diffusers import ControlNetModel
        if os.path.splitext(controlnet)[1] == ".safetensors":
            print(f"Loading ControlNet model from a file at: {controlnet}")
            controlnet = ControlNetModel.from_single_file(controlnet,
                                                          torch_dtype=torch.float16)
        else:
            controlnet = ControlNetModel.from_pretrained(controlnet,
                                                         torch_dtype=torch.float16)
        extra_kwargs['controlnet'] = controlnet
    model = pipeline_cls.from_pretrained(model,
                                         torch_dtype=torch.float16,
                                         **extra_kwargs)
    
    if vae is not None:
        from diffusers import AutoencoderKL
        model.vae = AutoencoderKL.from_pretrained(vae, torch_dtype=torch.float16)
    
    if scheduler is not None:
        scheduler_cls = getattr(importlib.import_module('diffusers'),
                                scheduler)
        model.scheduler = scheduler_cls.from_config(model.scheduler.config)
    if lora is not None:
        model.load_lora_weights(lora)
        model.fuse_lora()
    model.safety_checker = None
    model.to(torch.device('cuda'))
    return model


def compile_model(model):
    config = CompilationConfig.Default()

    # xformers and Triton are suggested for achieving best performance.
    # It might be slow for Triton to generate, compile and fine-tune kernels.
    try:
        import xformers
        config.enable_xformers = True
    except ImportError:
        print('xformers not installed, skip')
    # NOTE:
    # When GPU VRAM is insufficient or the architecture is too old, Triton might be slow.
    # Disable Triton if you encounter this problem.
    try:
        import triton
        config.enable_triton = True
    except ImportError:
        print('Triton not installed, skip')
    # NOTE:
    # CUDA Graph is suggested for small batch sizes and small resolutions to reduce CPU overhead.
    # My implementation can handle dynamic shape with increased need for GPU memory.
    # But when your GPU VRAM is insufficient or the image resolution is high,
    # CUDA Graph could cause less efficient VRAM utilization and slow down the inference,
    # especially when on Windows or WSL which has the "shared VRAM" mechanism.
    # If you meet problems related to it, you should disable it.
    config.enable_cuda_graph = True

    model = compile(model, config)
    return model


class IterationProfiler:

    def __init__(self):
        self.begin = None
        self.end = None
        self.num_iterations = 0

    def get_iter_per_sec(self):
        if self.begin is None or self.end is None:
            return None
        self.end.synchronize()
        dur = self.begin.elapsed_time(self.end)
        return self.num_iterations / dur * 1000.0

    def callback_on_step_end(self, pipe, i, t, callback_kwargs):
        if self.begin is None:
            event = torch.cuda.Event(enable_timing=True)
            event.record()
            self.begin = event
        else:
            event = torch.cuda.Event(enable_timing=True)
            event.record()
            self.end = event
            self.num_iterations += 1
        return callback_kwargs


def prepare_model(args: Namespace | None = None,
                  ) -> tuple[Any, dict[str, Any]]:
    begin_prep = time.time()
    
    if args is None:
        args = parse_args()
    
    if args.input_image is None:
        from diffusers import AutoPipelineForText2Image as pipeline_cls
    else:
        from diffusers import AutoPipelineForImage2Image as pipeline_cls

    model = load_model(
        pipeline_cls,
        args.model,
        variant=args.variant,
        custom_pipeline=args.custom_pipeline,
        scheduler=args.scheduler,
        lora=args.lora,
        controlnet=args.controlnet,
        vae=args.vae,
    )

    height = args.height or model.unet.config.sample_size * model.vae_scale_factor
    width = args.width or model.unet.config.sample_size * model.vae_scale_factor

    if args.quantize:

        def quantize_unet(m):
            from diffusers.utils import USE_PEFT_BACKEND
            assert USE_PEFT_BACKEND
            m = torch.quantization.quantize_dynamic(m, {torch.nn.Linear},
                                                    dtype=torch.qint8,
                                                    inplace=True)
            return m

        model.unet = quantize_unet(model.unet)
        if hasattr(model, 'controlnet'):
            model.controlnet = quantize_unet(model.controlnet)

    if args.no_fusion:
        torch.jit.set_fusion_strategy([('STATIC', 0), ('DYNAMIC', 0)])

    if args.compiler == 'none':
        pass
    elif args.compiler == 'sfast':
        model = compile_model(model)
    elif args.compiler in ('compile', 'compile-max-autotune'):
        mode = 'max-autotune' if args.compiler == 'compile-max-autotune' else None
        model.unet = torch.compile(model.unet, mode=mode)
        if hasattr(model, 'controlnet'):
            model.controlnet = torch.compile(model.controlnet, mode=mode)
        model.vae = torch.compile(model.vae, mode=mode)
    else:
        raise ValueError(f'Unknown compiler: {args.compiler}')

    if args.input_image is None:
        input_image = None
    else:
        input_image = load_image(args.input_image)
        input_image = input_image.resize((width, height),
                                         Image.LANCZOS)

    if args.control_image is None:
        if args.controlnet is None:
            control_image = None
        else:
            control_image = Image.new('RGB', (width, height))
            draw = ImageDraw.Draw(control_image)
            draw.ellipse((width // 4, height // 4,
                          width // 4 * 3, height // 4 * 3),
                         fill=(255, 255, 255))
            del draw
    else:
        control_image = load_image(args.control_image)
        control_image = control_image.resize((width, height),
                                             Image.LANCZOS)

    def get_kwarg_inputs():
        kwarg_inputs = dict(
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            height=height,
            width=width,
            num_inference_steps=args.steps,
            num_images_per_prompt=args.batch,
            generator=None if args.seed is None else torch.Generator(
                device='cuda').manual_seed(args.seed),
            **(dict() if args.extra_call_kwargs is None else json.loads(
                args.extra_call_kwargs)),
        )
        if input_image is not None:
            kwarg_inputs['image'] = input_image
        if control_image is not None:
            if input_image is None:
                kwarg_inputs['image'] = control_image
            else:
                kwarg_inputs['control_image'] = control_image
        return kwarg_inputs
    
    kwarg_inputs = get_kwarg_inputs()
    
    end_prep = time.time()
    print(f'Model preparation time: {end_prep - begin_prep:.3f}s')

    # NOTE: Warm it up.
    # The initial calls will trigger compilation and might be very slow.
    # After that, it should be very fast.
    if args.warmups > 0:
        begin_warmup = time.time()
        print('Begin warmup')
        
        for _ in range(args.warmups):
            model(**kwarg_inputs)
        
        end_warmup = time.time()
        print('End warmup')
        print(f'Warmup time ({args.warmups} times): {end_warmup - begin_warmup:.3f}s')
    
    return model, kwarg_inputs


def image_gen(
    model,
    kwarg_inputs: dict[str, ],
) -> list[Image.Image]:
    # Let's see it!
    # Note: Progress bar might work incorrectly due to the async nature of CUDA.
    iter_profiler = IterationProfiler()
    if 'callback_on_step_end' in inspect.signature(model).parameters:
        kwarg_inputs[
            'callback_on_step_end'] = iter_profiler.callback_on_step_end
    begin = time.time()
    output_images = model(**kwarg_inputs).images
    end = time.time()
    
    print(f'Inference time: {end - begin:.3f}s')
    iter_per_sec = iter_profiler.get_iter_per_sec()
    if iter_per_sec is not None:
        print(f'Iterations per second: {iter_per_sec:.3f}')
    peak_mem = torch.cuda.max_memory_allocated()
    print(f'Peak memory: {peak_mem / 1024**3:.3f}GiB')

    return output_images


if __name__ == '__main__':
    args = parse_args()
    model, kwarg_inputs = prepare_model(args=args)
    
    print("kwarg_inputs:")
    print(kwarg_inputs)
    
    output_images = image_gen(
        model=model,
        kwarg_inputs=kwarg_inputs,
    )
    
    if args.output_image is not None:
        base_path, ext = os.path.splitext(args.output_image)

        for i, image in enumerate(output_images):
            save_path = f"{base_path}-{i}{ext}"
            image.save(save_path)

    # Let's view it in terminal!
    if args.print_image_terminal:
        from sfast.utils.term_image import print_image

        for image in output_images:
            print_image(image, max_width=80)
