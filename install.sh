python3.10 -m venv venv

source venv/bin/activate

# Make sure Python version is 3.10
# If using different Python version,
# change the stable-fast prebuilt wheel accordingly
python -V

# Make sure CUDA version
nvcc --version

# This install is for Python3.10 + CUDA 11.8 + PyTorch 2.3.0
# Change them for different versions of CUDA/PyTorch/Python
python -m pip install --index-url https://download.pytorch.org/whl/cu118 \
  torch==2.3.0 xformers>=0.0.22 triton>=2.1.0 \
  https://github.com/chengzeyi/stable-fast/releases/download/v1.0.5/stable_fast-1.0.5+torch230cu118-cp310-cp310-manylinux2014_x86_64.whl

python -m pip install diffusers>=0.19.3 transformers accelerate peft
