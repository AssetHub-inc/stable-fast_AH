#include <torch/extension.h>

#include "jit/init.h"
#include "misc.h"
#include "operators/cudnn/cudnn_convolution.h"
#include "operators/cudnn/cudnn_qlinear.h"
#include "operators/cublas/cublas_gemm.h"
#include "operators/fused_linear.h"

namespace sfast {

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  jit::initJITBindings(m);
  misc::initMiscBindings(m);
}

TORCH_LIBRARY(sfast, m) {
  operators::initCUDNNConvolutionBindings(m);
  operators::initCUDNNQLinearBindings(m);
  operators::initCUBLASGEMMBindings(m);
  operators::initFusedLinearBindings(m);
}

} // namespace sfast
