# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

""" Quant Analyzer """

import os
from collections import OrderedDict, defaultdict
from typing import Union, Tuple, Callable, Dict, List
from bokeh import plotting
from bokeh import layouts
from bokeh.models import widgets
import torch

from aimet_common.utils import AimetLogger
from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_torch.utils import in_eval_mode, run_hook_for_layers_with_given_input
from aimet_torch.tensor_quantizer import TensorQuantizer
from aimet_torch.qc_quantize_op import QcQuantizeWrapper
from aimet_torch.qc_quantize_recurrent import QcQuantizeRecurrent
from aimet_torch.quantsim import QuantizationSimModel

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

DEFAULT_BOKEH_FIGURE_HEIGHT = 300


class CallbackFunc:
    """
    Class encapsulating callback function, and it's argument(s)
    """
    def __init__(self, func: Callable, func_callback_args=None):
        """
        :param func: Callable Function
        :param func_callback_args: Arguments passed to the callable function as-is.
        """
        self.func = func
        self.args = func_callback_args


class QuantAnalyzer:
    """
    QuantAnalyzer tool provides

     1) model sensitivity to weight and activation quantization
     2) per layer sensitivity analysis
     3) per layer encoding (min - max range) and PDF analysis and
     4) per layer MSE analysis
    """
    def __init__(
            self,
            model: torch.nn.Module,
            dummy_input: Union[torch.Tensor, Tuple],
            forward_pass_callback: CallbackFunc,
            eval_callback: CallbackFunc,
    ) -> None:
        """
        :param model: FP32 model to analyze for quantization.
        :param dummy_input: Dummy input to model.
        :param forward_pass_callback: A callback function for model calibration that simply runs
                forward passes on the model to compute encoding (delta/offset). This
                callback function should use representative data and should be subset of
                entire train/validation dataset (~1000 images/samples).
        :param eval_callback: A callback function for model evaluation that determines model
                performance. This callback function is expected to return scalar value
                representing the model performance evaluated against entire test/evaluation dataset.
        """
        if not isinstance(forward_pass_callback, CallbackFunc):
            raise ValueError('forward_pass_callback and its argument(s) are not encapsulated by CallbackFunc class.')
        if not isinstance(eval_callback, CallbackFunc):
            raise ValueError('eval_callback and its argument(s) are not encapsulated by CallbackFunc class.')

        self._model = model
        self._dummy_input = dummy_input
        self._forward_pass_callback = forward_pass_callback
        self._eval_callback = eval_callback

    def _eval_quantized_model(
            self,
            disable_param_quantizers: bool,
            disable_act_quantizers: bool,
            **kwargs) -> float:
        """
        Analyze model sensitivity to either parameter or activation quantization.

        :param disable_param_quantizers: Flag to disable all the param quantizers.
        :param disable_act_quantizers: Flag to disable all the activation quantizers.
        :param **kwargs: Additional arguments to the Quantsim.
        :return: Quantized model performance.
        """
        sim = QuantizationSimModel(self._model, self._dummy_input, **kwargs)

        if disable_param_quantizers:
            for quant_wrapper in sim.quant_wrappers():
                quant_wrapper.enable_param_quantizers(enabled=False)

        if disable_act_quantizers:
            for quant_wrapper in sim.quant_wrappers():
                quant_wrapper.enable_act_quantizers(enabled=False)

        sim.compute_encodings(self._forward_pass_callback.func, self._forward_pass_callback.args)
        acc = self._eval_model(sim.model)
        return acc

    def _eval_model(self, model: torch.nn.Module) -> float:
        """
        Evaluate the model performance.

        :param model: PyTorch model to be evaluated.
        :return: Scaler value representing model performance.
        """
        with in_eval_mode(model), torch.no_grad():
            return self._eval_callback.func(model, self._eval_callback.args)

    def _sort_quant_wrappers_based_on_occurrence(self, sim: QuantizationSimModel) -> Dict:
        """
        Sort quant wrappers based on occurrence for given quantsim model.

        :param sim: Quantsim model.
        :return: Ordered dictionary which maps wrapped module name to quant wrapper.
        """
        def sorting_hook(quant_wrapper: torch.nn.Module, *_):
            """
            Hook-function to sort quant wrappers based on occurrence.

            :param quant_wrapper: Quant wrapper.
            :param _: Additional args.
            """
            quant_wrapper_name = module_to_name_dict[quant_wrapper]
            sorted_quant_wrappers_dict[quant_wrapper_name] = quant_wrapper

        module_to_name_dict = {}
        for name, module in sim.model.named_modules():
            module_to_name_dict[module] = name

        sorted_quant_wrappers_dict = OrderedDict()
        run_hook_for_layers_with_given_input(sim.model, self._dummy_input, sorting_hook,
                                             module_type_for_attaching_hook=(QcQuantizeWrapper, QcQuantizeRecurrent),
                                             leaf_node_only=False)
        return sorted_quant_wrappers_dict

    @staticmethod
    def _get_enabled_quantizers(sorted_quant_wrappers: Dict)\
            -> Dict[Union[QcQuantizeWrapper, QcQuantizeRecurrent], List[TensorQuantizer]]:
        """
        For given sorted quant wrappers dict, get enabled quantizers.

        :param sorted_quant_wrappers: Dictionary containing quant wrappers sorted based on occurrence.
        :return: Dictionary which maps a quant wrapper to a list of enabled quantizers in it.
        """
        enabled_quant_wrappers = defaultdict(list)

        for quant_wrapper in sorted_quant_wrappers.values():
            for quantizer in quant_wrapper.param_quantizers.values():
                if quantizer.enabled:
                    enabled_quant_wrappers[quant_wrapper].append(quantizer)

            for quantizer in quant_wrapper.output_quantizers:
                if quantizer.enabled:
                    enabled_quant_wrappers[quant_wrapper].append(quantizer)

            for quantizer in quant_wrapper.input_quantizers:
                if quantizer.enabled:
                    enabled_quant_wrappers[quant_wrapper].append(quantizer)

        return enabled_quant_wrappers

    @staticmethod
    def _enable_quantizers(
            quantizers: List[TensorQuantizer],
            enabled: bool,
    ) -> None:
        """
        For given list of quantizers, set (enable/disable) quantizer's enabled.

        :param quantizers: List of quantizers.
        :param enabled: Enabled flag.
        """
        for quantizer in quantizers:
            quantizer.enabled = enabled

    def _perform_per_layer_analysis(
            self,
            sim: QuantizationSimModel,
            disable_all_quantizers: bool,
            enabled_before: bool,
            enabled_after: bool,
        ) -> Dict:
        """
        Helper function for perform_per_layer_analysis_by_enabling_quant_wrappers() and
        perform_per_layer_analysis_by_disabling_quant_wrappers()

        :param sim: Quantsim model.
        :param disable_all_quantizers: Flag to disable all the quantizers before per-layer analysis.
        :param enabled_before: Flag to set enabled for quantizers before computing encodings.
        :param enabled_after: Flag to set enabled for quantizers after computing encodings.
        :return: layer wise eval score dictionary. dict[layer_name] = eval_score.
        """
        # Sorted quant wrappers based on occurrence.
        # maps wrapped module name to a quant wrapper.
        sorted_quant_wrappers = self._sort_quant_wrappers_based_on_occurrence(sim)

        # Enabled quant wrappers.
        # maps quant wrapper to a list of enabled quantizers in it.
        enabled_quant_wrappers = self._get_enabled_quantizers(sorted_quant_wrappers)

        if disable_all_quantizers:
            for enabled_quantizers in enabled_quant_wrappers.values():
                self._enable_quantizers(enabled_quantizers, enabled=False)

        eval_score_dict = {}
        for name, quant_wrapper in sorted_quant_wrappers.items():
            if quant_wrapper in enabled_quant_wrappers:
                enabled_quantizers = enabled_quant_wrappers[quant_wrapper]
                self._enable_quantizers(enabled_quantizers, enabled=enabled_before)

                # Compute encodings and record eval score.
                sim.compute_encodings(self._forward_pass_callback.func, self._forward_pass_callback.args)
                eval_score_dict[name] = self._eval_model(sim.model)
                _logger.info("For layer: %s, the eval score is: %.02f", name, eval_score_dict[name])

                self._enable_quantizers(enabled_quantizers, enabled=enabled_after)

        return eval_score_dict

    @staticmethod
    def _create_per_layer_sensitivity_analysis_plot(layer_wise_eval_score_dict: Dict, title: str)\
            -> plotting.Figure:
        """
        Export diagnostics in html format.

        :param layer_wise_eval_score_dict: layer wise eval score dictionary. dict[layer_name] = eval_score
        :param title: Title of the plot.
        """
        layer_names = []
        eval_scores = []
        for layer_name, eval_score in layer_wise_eval_score_dict.items():
            layer_names.append(layer_name)
            eval_scores.append(eval_score)

        plot = plotting.figure(x_range=layer_names,
                               height=DEFAULT_BOKEH_FIGURE_HEIGHT,
                               title=title,
                               x_axis_label="Layers",
                               y_axis_label="Eval score")

        plot.line(x=layer_names, y=eval_scores)
        plot.y_range.start = 0
        return plot

    def _check_model_sensitivity_to_quantization(
            self,
            quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
            rounding_mode: str = 'nearest',
            default_param_bw: int = 8,
            default_output_bw: int = 8,
            config_file: str = None,
            default_data_type: QuantizationDataType = QuantizationDataType.int,
    ) -> Tuple[float, float, float]:
        """
        Perform the sensitivity analysis to weight and activation quantization
        individually.

        :param quant_scheme: Quantization scheme. Supported values are
                QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced.
        :param rounding_mode: Rounding mode. Supported options are 'nearest' or 'stochastic'.
        :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters.
        :param default_output_bw: Default bitwidth (4-31) to use for quantizing layer inputs and outputs.
        :param config_file: Path to configuration file for model quantizers.
        :param default_data_type: Default data type to use for quantizing all layer inputs, outputs and parameters.
                                 Possible options are QuantizationDataType.int and QuantizationDataType.float.
                                 Note that the mode default_data_type=QuantizationDataType.float is only supported with
                                 default_output_bw=16 and default_param_bw=16.
        :return: FP32 eval score, weight-quantized eval score, act-quantized eval score.
        """
        kwargs = dict(
            quant_scheme=quant_scheme,
            rounding_mode=rounding_mode,
            default_output_bw=default_output_bw,
            default_param_bw=default_param_bw,
            config_file=config_file,
            default_data_type=default_data_type,
        )
        fp32_eval_score = self._eval_model(self._model)
        _logger.info("FP32 eval score (W32A32): %.02f", fp32_eval_score)

        weight_quantized_eval_score = self._eval_quantized_model(disable_param_quantizers=False,
                                                                 disable_act_quantizers=True,
                                                                 **kwargs)
        _logger.info("Weight-quantized eval score (W%dA32): %.02f", default_param_bw, weight_quantized_eval_score)

        act_quantized_eval_score = self._eval_quantized_model(disable_param_quantizers=True,
                                                              disable_act_quantizers=False,
                                                              **kwargs)
        _logger.info("Activation-quantized eval score (W32A%d): %.02f", default_output_bw, act_quantized_eval_score)

        return fp32_eval_score, weight_quantized_eval_score, act_quantized_eval_score

    def _perform_per_layer_analysis_by_enabling_quant_wrappers(
            self,
            quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
            rounding_mode: str = 'nearest',
            default_param_bw: int = 8,
            default_output_bw: int = 8,
            config_file: str = None,
            default_data_type: QuantizationDataType = QuantizationDataType.int,
    ) -> Dict:
        """
        NOTE: Option 1

        1. All quant wrappers' parameters and activations quantizers are disabled.
        2. For every quant wrappers, based on occurrence:
              i. Each quant wrapper's parameters and activations quantizers are enabled as per JSON config file
                 and set to bit-width specified.
             ii. Measure and record eval score on subset of dataset.
            iii. Disable enabled quantizers in step i.
        3. Returns dictionary containing quant wrapper name and corresponding eval score.

        :param quant_scheme: Quantization scheme. Supported values are
                QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced.
        :param rounding_mode: Rounding mode. Supported options are 'nearest' or 'stochastic'.
        :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters.
        :param default_output_bw: Default bitwidth (4-31) to use for quantizing layer inputs and outputs.
        :param config_file: Path to configuration file for model quantizers.
        :param default_data_type: Default data type to use for quantizing all layer inputs, outputs and parameters.
                                 Possible options are QuantizationDataType.int and QuantizationDataType.float.
                                 Note that the mode default_data_type=QuantizationDataType.float is only supported with
                                 default_output_bw=16 and default_param_bw=16.
        :return: layer wise eval score dictionary. dict[layer_name] = eval_score
        """
        kwargs = dict(
            quant_scheme=quant_scheme,
            rounding_mode=rounding_mode,
            default_output_bw=default_output_bw,
            default_param_bw=default_param_bw,
            config_file=config_file,
            default_data_type=default_data_type,
        )
        sim = QuantizationSimModel(self._model, self._dummy_input, **kwargs)

        _logger.info("\nOPTION-1:\nAll the quant wrappers are disabled.\n"
                     "Starting per-layer analysis by enabling quant wrappers as per config file.")
        layer_wise_eval_score_dict = self._perform_per_layer_analysis(sim,
                                                                      disable_all_quantizers=True,
                                                                      enabled_before=True,
                                                                      enabled_after=False)
        return layer_wise_eval_score_dict

    def _perform_per_layer_analysis_by_disabling_quant_wrappers(
            self,
            quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
            rounding_mode: str = 'nearest',
            default_param_bw: int = 8,
            default_output_bw: int = 8,
            config_file: str = None,
            default_data_type: QuantizationDataType = QuantizationDataType.int,
    ) -> Dict:
        """
        NOTE: Option 2

        1. All quant wrappers' parameters and activations quantizers are enabled as per JSON config file
        and set to bit-width specified.
        2. For every quant wrappers, based on occurrence:
              i. Each quant wrapper's parameters and activations quantizers are disabled.
             ii. Measure and record eval score on subset of dataset.
            iii. Enable disabled quantizers in step i.
        3. Returns dictionary containing quant wrapper name and corresponding eval score.

        :param quant_scheme: Quantization scheme. Supported values are
                QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced.
        :param rounding_mode: Rounding mode. Supported options are 'nearest' or 'stochastic'.
        :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters.
        :param default_output_bw: Default bitwidth (4-31) to use for quantizing layer inputs and outputs.
        :param config_file: Path to configuration file for model quantizers.
        :param default_data_type: Default data type to use for quantizing all layer inputs, outputs and parameters.
                                 Possible options are QuantizationDataType.int and QuantizationDataType.float.
                                 Note that the mode default_data_type=QuantizationDataType.float is only supported with
                                 default_output_bw=16 and default_param_bw=16.
        :return: layer wise eval score dictionary. dict[layer_name] = eval_score
        """
        kwargs = dict(
            quant_scheme=quant_scheme,
            rounding_mode=rounding_mode,
            default_output_bw=default_output_bw,
            default_param_bw=default_param_bw,
            config_file=config_file,
            default_data_type=default_data_type,
        )
        sim = QuantizationSimModel(self._model, self._dummy_input, **kwargs)

        _logger.info("\nOPTION-2:\nAll the quant wrappers are enabled as per config file.\n"
                     "Starting per-layer analysis by disabling quant wrappers.")
        layer_wise_eval_score_dict = self._perform_per_layer_analysis(sim,
                                                                      disable_all_quantizers=False,
                                                                      enabled_before=False,
                                                                      enabled_after=True)
        return layer_wise_eval_score_dict

    def analyze( # pylint: disable=too-many-locals
            self,
            quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
            rounding_mode: str = 'nearest',
            default_param_bw: int = 8,
            default_output_bw: int = 8,
            config_file: str = None,
            default_data_type: QuantizationDataType = QuantizationDataType.int,
            results_dir: str = "./tmp/",
    ) -> None:
        """
        Analyze model sensitivity to quantization, perform per layer sensitivity analysis.

        :param quant_scheme: Quantization scheme. Supported values are
                QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced.
        :param rounding_mode: Rounding mode. Supported options are 'nearest' or 'stochastic'.
        :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters.
        :param default_output_bw: Default bitwidth (4-31) to use for quantizing layer inputs and outputs.
        :param config_file: Path to configuration file for model quantizers.
        :param default_data_type: Default data type to use for quantizing all layer inputs, outputs and parameters.
                                 Possible options are QuantizationDataType.int and QuantizationDataType.float.
                                 Note that the mode default_data_type=QuantizationDataType.float is only supported with
                                 default_output_bw=16 and default_param_bw=16.
        :param results_dir: Directory to save the results.
        """
        kwargs = dict(
            quant_scheme=quant_scheme,
            rounding_mode=rounding_mode,
            default_output_bw=default_output_bw,
            default_param_bw=default_param_bw,
            config_file=config_file,
            default_data_type=default_data_type,
        )

        results_dir = os.path.abspath(results_dir)
        os.makedirs(results_dir, exist_ok=True)

        # Configure the output file to be saved.
        filename = os.path.join(results_dir, "per_layer_sensitivity_analysis.html")
        plotting.output_file(filename)

        fp32_acc, weight_quantized_acc, act_quantized_acc = self._check_model_sensitivity_to_quantization(**kwargs)

        # Create block of pre-formatted text.
        text = f"FP32 eval score (W32A32):  {fp32_acc}\n" \
               f"Weight-quantized eval score (W{default_param_bw}A32):  {weight_quantized_acc}\n" \
               f"Activation-quantized eval score (W32A{default_output_bw}): {act_quantized_acc}\n"
        preformatted_text = widgets.PreText(text=text)

        layer_wise_eval_score_dict = self._perform_per_layer_analysis_by_enabling_quant_wrappers(**kwargs)
        plot1 = self._create_per_layer_sensitivity_analysis_plot(layer_wise_eval_score_dict,
                                                                 title="OPTION 1:"
                                                                       " Enabling each quant wrapper,"
                                                                       " remaining quant wrappers are disabled.")

        layer_wise_eval_score_dict = self._perform_per_layer_analysis_by_disabling_quant_wrappers(**kwargs)
        plot2 = self._create_per_layer_sensitivity_analysis_plot(layer_wise_eval_score_dict,
                                                                 title="OPTION 2:"
                                                                       " Disabling each quant wrapper,"
                                                                       " remaining quant wrappers are enabled.")
        # Create column of preformatted_text, plot1 and plot2 objects.
        # Save column object data in the html file format.
        plotting.save(layouts.column(preformatted_text, plot1, plot2))
