# Owner(s): ["oncall: quantization"]
import copy
import torch
import torch._dynamo as torchdynamo
import torch.nn as nn
from torch.ao.quantization._pt2e.quantizer import (
    X86InductorQuantizer,
)
from torch.ao.quantization._quantize_pt2e import (
    convert_pt2e,
    prepare_pt2e_quantizer,
)
from torch.testing._internal.common_quantization import (
    NodeSpec as ns,
    QuantizationTestCase,
    skipIfNoX86,
    skipIfNoDynamoSupport,
)
from torch.testing._internal.common_quantized import override_quantized_engine
from enum import Enum
import itertools

@skipIfNoDynamoSupport
class TestQuantizePT2EX86Inductor(QuantizationTestCase):
    @skipIfNoX86
    def test_conv2d_with_quantizer_api(self):
        class Mod(torch.nn.Module):
            def __init__(self, ) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 6, (2, 2), stride=(1, 1), padding=(1, 1))

            def forward(self, x):
                return self.conv(x)

        with override_quantized_engine("x86"):
            with torch.no_grad():
                m = Mod().eval()
                m_copy = copy.deepcopy(m)
                example_inputs = (torch.randn(2, 3, 16, 16),)
                # program capture
                m, guards = torchdynamo.export(
                    m,
                    *copy.deepcopy(example_inputs),
                    aten_graph=True,
                )

                before_fusion_result = m(*example_inputs)
                import torch.ao.quantization._pt2e.quantizer.x86_inductor_quantizer as xiq
                quantizer = X86InductorQuantizer()
                operator_config = xiq.get_default_x86_inductor_quantization_config()
                quantizer.set_global(operator_config)
                # Insert Observer
                m = prepare_pt2e_quantizer(m, quantizer)
                after_prepare_result = m(*example_inputs)
                m = convert_pt2e(m)
                node_occurrence = {
                    # one for input and weight of the conv, one for output for the conv
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 2,
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 2,
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 1,
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 1,
                }
                node_list = [
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ns.call_function(torch.ops.aten.convolution.default),
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                ]
                self.checkGraphModuleNodes(m,
                                           expected_node_occurrence=node_occurrence,
                                           expected_node_list=node_list)

    @skipIfNoX86
    def test_conv2d_unary_with_quantizer_api(self):
        class Mod(torch.nn.Module):
            def __init__(self, inplace_relu: bool = False, use_bias: bool = False) -> None:
                super().__init__()
                self.conv = nn.Conv2d(3, 6, (2, 2), stride=(1, 1), padding=(1, 1), bias=use_bias)
                self.relu = nn.ReLU(inplace=inplace_relu)

            def forward(self, x):
                return self.relu(self.conv(x))

        inplace_relu_list = [True, False]
        use_bias_list = [True, False]
        with override_quantized_engine("x86"):
            with torch.no_grad():
                for inplace_relu, use_bias in itertools.product(inplace_relu_list, use_bias_list):
                    m = Mod(inplace_relu=inplace_relu, use_bias=use_bias).eval()
                    m_copy = copy.deepcopy(m)
                    example_inputs = (torch.randn(2, 3, 16, 16),)
                    # program capture
                    m, guards = torchdynamo.export(
                        m,
                        *copy.deepcopy(example_inputs),
                        aten_graph=True,
                    )

                    before_fusion_result = m(*example_inputs)
                    import torch.ao.quantization._pt2e.quantizer.x86_inductor_quantizer as xiq
                    quantizer = X86InductorQuantizer()
                    operator_spec = xiq.get_default_x86_inductor_quantization_config()
                    quantizer.set_global(operator_spec)
                    # Insert Observer
                    m = prepare_pt2e_quantizer(m, quantizer)
                    after_prepare_result = m(*example_inputs)
                    m = convert_pt2e(m)
                    node_occurrence = {
                        # one for input and weight of the conv, one for output for the relu
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 2,
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 2,
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 1,
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 1,
                    }
                    node_list = [
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                        ns.call_function(torch.ops.aten.convolution.default),
                        ns.call_function(torch.ops.aten.relu_.default if inplace_relu else torch.ops.aten.relu.default),
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ]
                    self.checkGraphModuleNodes(m,
                                               expected_node_occurrence=node_occurrence,
                                               expected_node_list=node_list)

    @skipIfNoX86
    def test_conv2d_binary_with_quantizer_api(self):
        class Conv2DType(Enum):
            left = 1
            right = 2
            both = 3

        class Mod(torch.nn.Module):
            def __init__(self,
                         inplace_add: bool = False,
                         conv2d_type: Conv2DType = Conv2DType.left,
                         use_bias: bool = False,
                         ) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=use_bias
                )
                self.conv2 = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=use_bias
                )
                self.relu = nn.ReLU()
                self.inplace_add = inplace_add
                self.conv2d_type = conv2d_type

            def forward(self, x):
                if self.conv2d_type == Conv2DType.left:
                    if self.inplace_add:
                        tmp = self.conv(x)
                        tmp += self.relu(x)
                        return tmp
                    else:
                        return self.conv(x) + self.relu(x)
                elif self.conv2d_type == Conv2DType.right:
                    if self.inplace_add:
                        tmp = self.relu(x)
                        tmp += self.conv(x)
                        return tmp
                    else:
                        return self.relu(x) + self.conv(x)
                elif self.conv2d_type == Conv2DType.both:
                    if self.inplace_add:
                        tmp = self.conv(x)
                        tmp += self.conv2(x)
                        return tmp
                    else:
                        return self.conv(x) + self.conv2(x)


        inplace_add_list = [True, False]
        conv2d_type_list = [Conv2DType.left, Conv2DType.right, Conv2DType.both]
        use_bias_list = [True, False]

        with override_quantized_engine("x86"):
            with torch.no_grad():
                for inplace_add, conv2d_type, use_bias in itertools.product(inplace_add_list, conv2d_type_list, use_bias_list):
                    m = Mod(inplace_add=inplace_add, conv2d_type=conv2d_type, use_bias=use_bias).eval()
                    m_copy = copy.deepcopy(m)
                    example_inputs = (torch.randn(2, 3, 16, 16),)
                    # program capture
                    m, guards = torchdynamo.export(
                        m,
                        *copy.deepcopy(example_inputs),
                        aten_graph=True,
                    )

                    before_fusion_result = m(*example_inputs)
                    import torch.ao.quantization._pt2e.quantizer.x86_inductor_quantizer as xiq
                    quantizer = X86InductorQuantizer()
                    operator_spec = xiq.get_default_x86_inductor_quantization_config()
                    quantizer.set_global(operator_spec)
                    # Insert Observer
                    m = prepare_pt2e_quantizer(m, quantizer)
                    after_prepare_result = m(*example_inputs)
                    m = convert_pt2e(m)
                    if conv2d_type != Conv2DType.both:
                        node_occurrence = {
                            # one for input and weight of the conv
                            # one for output for the add
                            # one for extra input node of add
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 3,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 3,
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 1,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 1,
                        }
                    else:
                        node_occurrence = {
                            # one for input and weight of the conv
                            # one for input and weight of another conv
                            # one for output for the add
                            # 2 conv will share same input quant/dequant
                            # one for extra input node of add
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 4,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 4,
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 2,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 2,
                        }
                    node_list = [
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                        ns.call_function(torch.ops.aten.convolution.default),
                        ns.call_function(torch.ops.aten.add_.Tensor if inplace_add else torch.ops.aten.add.Tensor),
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ]
                    self.checkGraphModuleNodes(m,
                                               expected_node_occurrence=node_occurrence,
                                               expected_node_list=node_list)

    @skipIfNoX86
    def test_conv2d_binary_unary_with_quantizer_api(self):
        class Conv2DType(Enum):
            left = 1
            right = 2
            both = 3

        class Mod(torch.nn.Module):
            def __init__(self,
                         inplace_add: bool = False,
                         conv2d_type: Conv2DType = Conv2DType.left,
                         inplace_relu: bool = False,
                         use_bias: bool = False,
                         ) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=use_bias
                )
                self.conv2 = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=use_bias
                )
                self.relu = nn.ReLU()
                self.inplace_add = inplace_add
                self.conv2d_type = conv2d_type
                self.relu2 = nn.ReLU(inplace=inplace_relu)

            def forward(self, x):
                if self.conv2d_type == Conv2DType.left:
                    if self.inplace_add:
                        tmp = self.conv(x)
                        tmp += self.relu(x)
                        return self.relu2(tmp)
                    else:
                        return self.relu2(self.conv(x) + self.relu(x))
                elif self.conv2d_type == Conv2DType.right:
                    if self.inplace_add:
                        tmp = self.relu(x)
                        tmp += self.conv(x)
                        return self.relu2(tmp)
                    else:
                        return self.relu2(self.relu(x) + self.conv(x))
                elif self.conv2d_type == Conv2DType.both:
                    if self.inplace_add:
                        tmp = self.conv(x)
                        tmp += self.conv2(x)
                        return self.relu2(tmp)
                    else:
                        return self.relu2(self.conv(x) + self.conv2(x))

        inplace_add_list = [True, False]
        conv2d_type_list = [Conv2DType.left, Conv2DType.right, Conv2DType.both]
        inplace_relu_list = [True, False]
        use_bias_list = [True, False]

        with override_quantized_engine("x86"):
            with torch.no_grad():
                for inplace_add, conv2d_type, inplace_relu, use_bias in itertools.product(
                        inplace_add_list,
                        conv2d_type_list,
                        inplace_relu_list,
                        use_bias_list,
                ):
                    m = Mod(inplace_add=inplace_add, conv2d_type=conv2d_type, inplace_relu=inplace_relu, use_bias=use_bias).eval()
                    m_copy = copy.deepcopy(m)
                    example_inputs = (torch.randn(2, 3, 16, 16),)
                    # program capture
                    m, guards = torchdynamo.export(
                        m,
                        *copy.deepcopy(example_inputs),
                        aten_graph=True,
                    )

                    before_fusion_result = m(*example_inputs)
                    import torch.ao.quantization._pt2e.quantizer.x86_inductor_quantizer as xiq
                    quantizer = X86InductorQuantizer()
                    operator_spec = xiq.get_default_x86_inductor_quantization_config()
                    quantizer.set_global(operator_spec)
                    # Insert Observer
                    m = prepare_pt2e_quantizer(m, quantizer)
                    after_prepare_result = m(*example_inputs)
                    m = convert_pt2e(m)
                    if conv2d_type != Conv2DType.both:
                        node_occurrence = {
                            # one for input and weight of the conv
                            # one for output for the relu
                            # one for extra input node of add
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 3,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 3,
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 1,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 1,
                        }
                    else:
                        node_occurrence = {
                            # one for input and weight of the conv
                            # one for input and weight of another conv
                            # one for output for the relu
                            # 2 conv will share same input quant/dequant
                            # one for extra input node of add
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 4,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 4,
                            ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 2,
                            ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 2,
                        }
                    node_list = [
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                        ns.call_function(torch.ops.aten.convolution.default),
                        ns.call_function(torch.ops.aten.add_.Tensor if inplace_add else torch.ops.aten.add.Tensor),
                        ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                        ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ]
                    self.checkGraphModuleNodes(m,
                                               expected_node_occurrence=node_occurrence,
                                               expected_node_list=node_list)

    @skipIfNoX86
    def test_conv2d_serials_binary_unary_with_quantizer_api(self):
        class Mod(torch.nn.Module):
            def __init__(self, ) -> None:
                super().__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=True
                )
                self.conv2 = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=True
                )
                self.conv3 = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=True
                )
                self.conv4 = torch.nn.Conv2d(
                    in_channels=3, out_channels=3, kernel_size=3, stride=1, padding=1, bias=True
                )
                self.relu = nn.ReLU()
                self.relu2 = nn.ReLU()

            def forward(self, x):
                x1 = self.conv(x)
                res1 = self.relu(self.conv2(x1) + self.conv3(x1))
                res2 = self.relu2(self.conv4(res1) + res1)
                return res2

        with override_quantized_engine("x86"):
            with torch.no_grad():
                m = Mod().eval()
                m_copy = copy.deepcopy(m)
                example_inputs = (torch.randn(2, 3, 16, 16),)
                # program capture
                m, guards = torchdynamo.export(
                    m,
                    *copy.deepcopy(example_inputs),
                    aten_graph=True,
                )

                before_fusion_result = m(*example_inputs)
                import torch.ao.quantization._pt2e.quantizer.x86_inductor_quantizer as xiq
                quantizer = X86InductorQuantizer()
                operator_config = xiq.get_default_x86_inductor_quantization_config()
                quantizer.set_global(operator_config)
                # Insert Observer
                m = prepare_pt2e_quantizer(m, quantizer)
                after_prepare_result = m(*example_inputs)
                m = convert_pt2e(m)
                node_occurrence = {
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 5,
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 5,
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_channel.default): 4,
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_channel.default): 4,
                }
                node_list = [
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ns.call_function(torch.ops.aten.convolution.default),
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                    ns.call_function(torch.ops.aten.convolution.default),
                    ns.call_function(torch.ops.aten.convolution.default),
                    ns.call_function(torch.ops.aten.add.Tensor),
                    ns.call_function(torch.ops.aten.relu.default),
                    ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default),
                    ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default),
                ]
                self.checkGraphModuleNodes(m,
                                           expected_node_occurrence=node_occurrence,
                                           expected_node_list=node_list)
