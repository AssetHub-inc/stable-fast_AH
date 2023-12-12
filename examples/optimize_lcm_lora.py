MODEL = 'runwayml/stable-diffusion-v1-5'
VARIANT = None
CUSTOM_PIPELINE = None
SCHEDULER = 'LCMScheduler'
LORA = 'latent-consistency/lcm-lora-sdv1-5'
CONTROLNET = None
STEPS = 4
PROMPT = 'best quality, realistic, unreal engine, 4K, a beautiful girl'
SEED = None
WARMUPS = 3
BATCH = 1
HEIGHT = 512
WIDTH = 512
EXTRA_CALL_KWARGS = '{"guidance_scale": 0.0}'

import importlib
import inspect
import argparse
import time
import json
import torch
from PIL import (Image, ImageDraw)
from sfast.compilers.stable_diffusion_pipeline_compiler import (
    compile, CompilationConfig)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default=MODEL)
    parser.add_argument('--variant', type=str, default=VARIANT)
    parser.add_argument('--custom-pipeline', type=str, default=CUSTOM_PIPELINE)
    parser.add_argument('--scheduler', type=str, default=SCHEDULER)
    parser.add_argument('--lora', type=str, default=LORA)
    parser.add_argument('--controlnet', type=str, default=None)
    parser.add_argument('--steps', type=int, default=STEPS)
    parser.add_argument('--prompt', type=str, default=PROMPT)
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--warmups', type=int, default=WARMUPS)
    parser.add_argument('--batch', type=int, default=BATCH)
    parser.add_argument('--height', type=int, default=HEIGHT)
    parser.add_argument('--width', type=int, default=WIDTH)
    parser.add_argument('--extra-call-kwargs',
                        type=str,
                        default=EXTRA_CALL_KWARGS)
    parser.add_argument('--input-image', type=str, default=None)
    parser.add_argument('--control-image', type=str, default=None)
    parser.add_argument('--output-image', type=str, default=None)
    parser.add_argument(
        '--compiler',
        type=str,
        default='sfast',
        choices=['none', 'sfast', 'compile', 'compile-max-autotune'])
    return parser.parse_args()


def load_model(pipeline_cls,
               model,
               variant=None,
               custom_pipeline=None,
               scheduler=None,
               lora=None,
               controlnet=None):
    extra_kwargs = {}
    if custom_pipeline is not None:
        extra_kwargs['custom_pipeline'] = custom_pipeline
    if variant is not None:
        extra_kwargs['variant'] = variant
    if controlnet is not None:
        from diffusers import ControlNetModel
        controlnet = ControlNetModel.from_pretrained(controlnet,
                                                     torch_dtype=torch.float16)
        extra_kwargs['controlnet'] = controlnet
    model = pipeline_cls.from_pretrained(model,
                                         torch_dtype=torch.float16,
                                         **extra_kwargs)
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


def main():
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
    )
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
        input_image = Image.open(args.input_image).convert('RGB')
        input_image = input_image.resize((args.width, args.height),
                                         Image.LANCZOS)

    if args.control_image is None:
        if args.controlnet is None:
            control_image = None
        else:
            control_image = Image.new('RGB', (args.width, args.height))
            draw = ImageDraw.Draw(control_image)
            draw.ellipse((args.width // 4, args.height // 4,
                          args.width // 4 * 3, args.height // 4 * 3),
                         fill=(255, 255, 255))
            del draw
    else:
        control_image = Image.open(args.control_image).convert('RGB')
        control_image = control_image.resize((args.width, args.height),
                                             Image.LANCZOS)

    def get_kwarg_inputs():
        kwarg_inputs = dict(
            prompt=args.prompt,
            height=args.height,
            width=args.width,
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

    # NOTE: Warm it up.
    # The initial calls will trigger compilation and might be very slow.
    # After that, it should be very fast.
    for _ in range(args.warmups):
        model(**get_kwarg_inputs())

    # Let's see it!
    # Note: Progress bar might work incorrectly due to the async nature of CUDA.
    kwarg_inputs = get_kwarg_inputs()
    iter_profiler = None
    if 'callback_on_step_end' in inspect.signature(model).parameters:
        iter_profiler = IterationProfiler()
        kwarg_inputs[
            'callback_on_step_end'] = iter_profiler.callback_on_step_end
    begin = time.time()
    output_images = model(**kwarg_inputs).images
    end = time.time()

    # Let's view it in terminal!
    from sfast.utils.term_image import print_image

    for image in output_images:
        print_image(image, max_width=80)

    print(f'Inference time: {end - begin:.3f}s')
    iter_per_sec = iter_profiler.get_iter_per_sec()
    if iter_per_sec is not None:
        print(f'Iterations per second: {iter_per_sec:.3f}')

    if args.output_image is not None:
        output_images[0].save(args.output_image)


if __name__ == '__main__':
    main()