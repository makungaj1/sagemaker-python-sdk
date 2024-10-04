# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Placeholder docstring"""
from __future__ import absolute_import

import logging
import os
import shutil
import tempfile
from typing import Union, Optional, Dict
from urllib.request import urlretrieve

from omegaconf import OmegaConf
from packaging.version import Version

from sagemaker.estimator import Framework, EstimatorBase
from sagemaker.fw_utils import (
    framework_name_from_image,
    framework_version_from_tag,
    python_deprecation_warning,
    validate_version_or_image_args,
    validate_distribution,
    profiler_config_deprecation_warning,
)
from sagemaker.git_utils import _run_clone_command
from sagemaker.pytorch import defaults
from sagemaker.pytorch.model import PyTorchModel
from sagemaker.pytorch.training_compiler.config import TrainingCompilerConfig
from sagemaker.vpc_utils import VPC_CONFIG_DEFAULT
from sagemaker.workflow.entities import PipelineVariable

logger = logging.getLogger("sagemaker")


class PyTorch(Framework):
    """Handle end-to-end training and deployment of custom PyTorch code."""

    _framework_name = "pytorch"
    LAUNCH_PYTORCH_DDP_ENV_NAME = "sagemaker_pytorch_ddp_enabled"
    LAUNCH_TORCH_DISTRIBUTED_ENV_NAME = "sagemaker_torch_distributed_enabled"
    INSTANCE_TYPE_ENV_NAME = "sagemaker_instance_type"

    # [TODO] Add image uris to image_uri_config/_.json and use image_uris.retrieve
    # to retrieve the image uri below before GA.
    SM_ADAPTER_REPO = "git@github.com:aws/private-sagemaker-training-adapter-for-nemo-staging.git"
    SM_LAUNCHER_REPO = "git@github.com:aws/private-sagemaker-training-launcher-staging.git"
    SM_TRAINING_RECIPE_GPU_IMG = (
        "855988369404.dkr.ecr.us-west-2.amazonaws.com/chinmayee-dev:adaptor_sept9_v1"
    )
    SM_NEURONX_DIST_REPO = "https://github.com/aws-neuron/neuronx-distributed-training.git"
    SM_NEURONX_DIST_IMG = (
        "855988369404.dkr.ecr.us-west-2.amazonaws.com/chinmayee-dev:neuron_sept26_v1"
    )

    def __init__(
        self,
        entry_point: Optional[Union[str, PipelineVariable]] = None,
        framework_version: Optional[str] = None,
        py_version: Optional[str] = None,
        source_dir: Optional[Union[str, PipelineVariable]] = None,
        hyperparameters: Optional[Dict[str, Union[str, PipelineVariable]]] = None,
        image_uri: Optional[Union[str, PipelineVariable]] = None,
        distribution: Optional[Dict] = None,
        compiler_config: Optional[TrainingCompilerConfig] = None,
        training_recipe: Optional[str] = None,
        recipe_overrides: Optional[Dict] = None,
        **kwargs,
    ):
        """This ``Estimator`` executes a PyTorch script in a managed PyTorch execution environment.

        The managed PyTorch environment is an Amazon-built Docker container that executes functions
        defined in the supplied ``entry_point`` Python script within a SageMaker Training Job.

        Training is started by calling
        :meth:`~sagemaker.amazon.estimator.Framework.fit` on this Estimator.
        After training is complete, calling
        :meth:`~sagemaker.amazon.estimator.Framework.deploy` creates a hosted
        SageMaker endpoint and returns an
        :class:`~sagemaker.amazon.pytorch.model.PyTorchPredictor` instance that
        can be used to perform inference against the hosted model.

        Technical documentation on preparing PyTorch scripts for SageMaker
        training and using the PyTorch Estimator is available on the project
        home-page: https://github.com/aws/sagemaker-python-sdk

        Args:
            entry_point (str or PipelineVariable): Path (absolute or relative) to the
                Python source file which should be executed as the entry point to training.
                If ``source_dir`` is specified, then ``entry_point``
                must point to a file located at the root of ``source_dir``.
            framework_version (str): PyTorch version you want to use for
                executing your model training code. Defaults to ``None``. Required unless
                ``image_uri`` is provided. List of supported versions:
                https://github.com/aws/deep-learning-containers/blob/master/available_images.md.
            py_version (str): Python version you want to use for executing your
                model training code. One of 'py2' or 'py3'. Defaults to ``None``. Required
                unless ``image_uri`` is provided.
            source_dir (str or PipelineVariable): Path (absolute, relative or an S3 URI) to
                a directory with any other training source code dependencies aside from the entry
                point file (default: None). If ``source_dir`` is an S3 URI, it must
                point to a tar.gz file. Structure within this directory are preserved
                when training on Amazon SageMaker.
            hyperparameters (dict[str, str] or dict[str, PipelineVariable]): Hyperparameters
                that will be used for training (default: None). The hyperparameters are made
                accessible as a dict[str, str] to the training code on
                SageMaker. For convenience, this accepts other types for keys
                and values, but ``str()`` will be called to convert them before
                training.
            image_uri (str or PipelineVariable): If specified, the estimator will use this image
                for training and hosting, instead of selecting the appropriate
                SageMaker official image based on framework_version and
                py_version. It can be an ECR url or dockerhub image and tag.
                Examples:
                    * ``123412341234.dkr.ecr.us-west-2.amazonaws.com/my-custom-image:1.0``
                    * ``custom-image:latest``

                If ``framework_version`` or ``py_version`` are ``None``, then
                ``image_uri`` is required. If also ``None``, then a ``ValueError``
                will be raised.
            distribution (dict): A dictionary with information on how to configure and
                run distributed training
                (default: None). The following options are available.

                **To enable the SageMaker distributed data parallelism (SMDDP) library:**

                    .. code:: python

                        { "smdistributed": { "dataparallel": { "enabled": True } } }

                    Beside activating the SMDDP library through this parameter,
                    you also need to add few lines of code in your training script
                    for initializing PyTorch Distributed with the SMDDP setups.
                    To learn how to configure your training job with the SMDDP library v2, see
                    `Run distributed training with the SageMaker distributed data parallelism
                    library
                    <https://docs.aws.amazon.com/sagemaker/latest/dg/data-parallel.html>`_
                    in the *Amazon SageMaker User Guide*.

                **To enable the SageMaker distributed model parallelism (SMP) library v2:**

                    .. code:: python

                        {
                            "torch_distributed": { "enabled": True },
                            "smdistributed": {
                                "modelparallel": {
                                    "enabled": True,
                                    "parameters": {
                                        "tensor_parallel_degree": 8,
                                        "hybrid_shard_degree": 1,
                                        ...
                                    },
                                }
                            },
                        }

                    Beside activating the SMP library v2 through this parameter,
                    you also need to add few lines of code in your training script
                    for initializing PyTorch Distributed with the SMP setups.
                    To learn how to configure your training job with the SMP library v2, see
                    `Run distributed training with the SageMaker model parallelism library v2
                    <https://docs.aws.amazon.com/sagemaker/latest/dg/model-parallel-v2.html>`_
                    in the *Amazon SageMaker User Guide*.

                    .. note::

                        The SageMaker distributed model parallel library v2 requires with
                        ``torch_distributed``.

                    .. note::

                        The documentation for the SMP library v1.x is archived and available at
                        `Run distributed training with the SageMaker model parallelism library
                        <https://docs.aws.amazon.com/sagemaker/latest/dg/model-parallel.html>`_
                        in the *Amazon SageMaker User Guide*,
                        and the SMP v1 API reference is available in the
                        `SageMaker Python SDK v2.199.0 documentation
                        <https://sagemaker.readthedocs.io/en/v2.199.0/api/training/distributed.html#the-sagemaker-distributed-model-parallel-library>`_.

                **To enable PyTorch DDP:**

                    .. code:: python

                        {
                            "pytorchddp": {
                                "enabled": True
                            }
                        }

                    To learn more, see `Distributed PyTorch Training
                    <https://sagemaker.readthedocs.io/en/stable/frameworks/pytorch/using_pytorch.html#distributed-pytorch-training>`_.

                **To enable Torch Distributed:**

                    This is available for general distributed training on
                    GPU instances from PyTorch v1.13.1 and later.

                    .. code:: python

                        {
                            "torch_distributed": {
                                "enabled": True
                            }
                        }

                    This option also supports distributed training on Trn1.
                    To learn more, see `Distributed PyTorch Training on Trainium
                    <https://sagemaker.readthedocs.io/en/stable/frameworks/pytorch/using_pytorch.html#distributed-pytorch-training-on-trainium>`_.

                **To enable MPI:**

                    .. code:: python

                        {
                            "mpi": {
                                "enabled": True
                            }
                        }

                    To learn more, see `Training with Horovod
                    <https://sagemaker.readthedocs.io/en/stable/frameworks/tensorflow/using_tf.html#training-with-horovod>`_.

                **To enable parameter server:**

                    .. code:: python

                        {
                            "parameter_server": {
                                "enabled": True
                            }
                        }

                    To learn more, see `Training with parameter servers
                    <https://sagemaker.readthedocs.io/en/stable/frameworks/tensorflow/using_tf.html#training-with-parameter-servers>`_.

                **To enable distributed training with SageMaker Training Compiler:**

                    .. code:: python

                        {
                            "pytorchxla": {
                                "enabled": True
                            }
                        }

                    To learn more, see `SageMaker Training Compiler
                    <https://docs.aws.amazon.com/sagemaker/latest/dg/training-compiler.html>`_
                    in the *Amazon SageMaker Developer Guide*.

                    .. note::

                        When you use this PyTorch XLA option for distributed training strategy,
                        you must add the ``compiler_config`` parameter and activate SageMaker
                        Training Compiler.

                compiler_config (:class:`~sagemaker.pytorch.TrainingCompilerConfig`):
                Configures SageMaker Training Compiler to accelerate training.

            training_recipe (str): Training recipe to use. This is a local file path,
                                   a url to fetch, or a recipe provided by Saagemaker
                                   training.

            recipe_overrides (Dict): Dictionary specifying key values to override in the
                                     training_recipe.

            **kwargs: Additional kwargs passed to the :class:`~sagemaker.estimator.Framework`
                constructor.

        .. tip::

            You can find additional parameters for initializing this class at
            :class:`~sagemaker.estimator.Framework` and
            :class:`~sagemaker.estimator.EstimatorBase`.
        """
        if training_recipe is not None:
            if entry_point is not None:
                logger.warning("Argument entry_point will be ignored with training_recipe.")
            if source_dir is not None:
                logger.warning("Argument source_dir will be ignored with training_recipe.")
            if hyperparameters is not None:
                logger.warning("Argument hyperparameters will be ignored with training recipe.")
            if distribution is not None:
                logger.warning("Argument distribution will be ignored with training_recipe.")
            args = self._setup_for_training_recipe(training_recipe, recipe_overrides, kwargs)
            entry_point = args["entry_point"]
            source_dir = args["source_dir"]
            hyperparameters = args["hyperparameters"]
            if image_uri is None:
                image_uri = args["default_image_uri"]
            distribution = args["distribution"]
        elif entry_point is None:
            raise ValueError(
                "Argument entry_point must be set when training_recipe is not provided"
            )
        validate_version_or_image_args(framework_version, py_version, image_uri)
        if py_version == "py2":
            logger.warning(
                python_deprecation_warning(self._framework_name, defaults.LATEST_PY2_VERSION)
            )
        self.framework_version = framework_version
        self.py_version = py_version

        if "enable_sagemaker_metrics" not in kwargs:
            # enable sagemaker metrics for PT v1.3 or greater:
            if self.framework_version and Version(self.framework_version) >= Version("1.3"):
                kwargs["enable_sagemaker_metrics"] = True

        super(PyTorch, self).__init__(
            entry_point, source_dir, hyperparameters, image_uri=image_uri, **kwargs
        )

        if "entry_point" not in kwargs:
            kwargs["entry_point"] = entry_point

        if distribution is not None:
            # rewrite pytorchddp to smdistributed
            if "pytorchddp" in distribution:
                if "smdistributed" in distribution:
                    raise ValueError(
                        "Cannot use both pytorchddp and smdistributed "
                        "distribution options together.",
                        distribution,
                    )

                # convert pytorchddp distribution into smdistributed distribution
                distribution = distribution.copy()
                distribution["smdistributed"] = {"dataparallel": distribution["pytorchddp"]}
                del distribution["pytorchddp"]

            distribution = validate_distribution(
                distribution,
                self.instance_groups,
                self._framework_name,
                framework_version,
                py_version,
                image_uri,
                kwargs,
            )

        self.distribution = distribution or {}

        if compiler_config is not None:
            if not isinstance(compiler_config, TrainingCompilerConfig):
                error_string = (
                    f"Expected instance of type {TrainingCompilerConfig}"
                    f"for argument compiler_config. "
                    f"Instead got {type(compiler_config)}"
                )
                raise ValueError(error_string)
            if compiler_config:
                compiler_config.validate(self)
        elif distribution is not None and "pytorchxla" in distribution:
            raise ValueError(
                "Distributed training through PyTorch XLA is currently only supported "
                "when SageMaker Training Compiler is enabled. To learn more, "
                "see Enable SageMaker Training Compiler at "
                "https://docs.aws.amazon.com/sagemaker/latest/dg/training-compiler-enable.html."
            )
        self.compiler_config = compiler_config

        if "profiler_config" in kwargs:
            profiler_config_deprecation_warning(
                kwargs["profiler_config"], image_uri, self._framework_name, framework_version
            )

    def _pytorch_distribution_configuration(self, distribution):
        """Returns a dict of distribution config for PyTorch training

        Args:
            distribution (dict): A dictionary with information on how to run distributed training.
        Returns:
            dict containing Pytorch DDP config
        """
        distribution_config = {}
        pytorch_ddp_enabled = False
        torch_distributed_enabled = False

        if "pytorchddp" in distribution:
            pytorch_ddp_enabled = distribution.get("pytorchddp").get("enabled", False)
        elif "torch_distributed" in distribution:
            torch_distributed_enabled = distribution.get("torch_distributed").get("enabled", False)

        if pytorch_ddp_enabled:
            distribution_config[self.LAUNCH_PYTORCH_DDP_ENV_NAME] = pytorch_ddp_enabled
            if self.instance_type is not None:
                distribution_config[self.INSTANCE_TYPE_ENV_NAME] = self.instance_type
        elif torch_distributed_enabled:
            if "smdistributed" in distribution:
                # Enable torch_distributed for smdistributed.
                distribution_config = self._distribution_configuration(distribution=distribution)
            distribution_config[self.LAUNCH_TORCH_DISTRIBUTED_ENV_NAME] = torch_distributed_enabled
            if self.instance_type is not None:
                distribution_config[self.INSTANCE_TYPE_ENV_NAME] = self.instance_type
        else:
            distribution_config = self._distribution_configuration(distribution=distribution)

        return distribution_config

    def hyperparameters(self):
        """Return hyperparameters used by your custom PyTorch code during model training."""
        hyperparameters = super(PyTorch, self).hyperparameters()
        additional_hyperparameters = self._pytorch_distribution_configuration(
            distribution=self.distribution
        )
        hyperparameters.update(
            EstimatorBase._json_encode_hyperparameters(additional_hyperparameters)
        )
        if self.compiler_config:
            training_compiler_hyperparameters = self.compiler_config._to_hyperparameter_dict()
            hyperparameters.update(
                EstimatorBase._json_encode_hyperparameters(training_compiler_hyperparameters)
            )

        return hyperparameters

    def create_model(
        self,
        model_server_workers=None,
        role=None,
        vpc_config_override=VPC_CONFIG_DEFAULT,
        entry_point=None,
        source_dir=None,
        dependencies=None,
        **kwargs,
    ):
        """Create a SageMaker ``PyTorchModel`` object that can be deployed to an ``Endpoint``.

        Args:
            model_server_workers (int): Optional. The number of worker processes
                used by the inference server. If None, server will use one
                worker per vCPU.
            role (str): The ``ExecutionRoleArn`` IAM Role ARN for the ``Model``,
                which is also used during transform jobs. If not specified, the
                role from the Estimator will be used.
            vpc_config_override (dict[str, list[str]]): Optional override for VpcConfig set on
                the model. Default: use subnets and security groups from this Estimator.
                * 'Subnets' (list[str]): List of subnet ids.
                * 'SecurityGroupIds' (list[str]): List of security group ids.
            entry_point (str): Path (absolute or relative) to the local Python source file which
                should be executed as the entry point to training. If ``source_dir`` is specified,
                then ``entry_point`` must point to a file located at the root of ``source_dir``.
                If not specified, the training entry point is used.
            source_dir (str): Path (absolute or relative) to a directory with any other serving
                source code dependencies aside from the entry point file.
                If not specified, the model source directory from training is used.
            dependencies (list[str]): A list of paths to directories (absolute or relative) with
                any additional libraries that will be exported to the container.
                If not specified, the dependencies from training are used.
                This is not supported with "local code" in Local Mode.
            **kwargs: Additional kwargs passed to the :class:`~sagemaker.pytorch.model.PyTorchModel`
                constructor.

        Returns:
            sagemaker.pytorch.model.PyTorchModel: A SageMaker ``PyTorchModel``
            object. See :func:`~sagemaker.pytorch.model.PyTorchModel` for full details.
        """
        if "image_uri" not in kwargs:
            kwargs["image_uri"] = self.image_uri

        kwargs["name"] = self._get_or_create_name(kwargs.get("name"))

        return PyTorchModel(
            self.model_data,
            role or self.role,
            entry_point or self._model_entry_point(),
            framework_version=self.framework_version,
            py_version=self.py_version,
            source_dir=(source_dir or self._model_source_dir()),
            container_log_level=self.container_log_level,
            code_location=self.code_location,
            model_server_workers=model_server_workers,
            sagemaker_session=self.sagemaker_session,
            vpc_config=self.get_vpc_config(vpc_config_override),
            dependencies=(dependencies or self.dependencies),
            **kwargs,
        )

    @classmethod
    def _prepare_init_params_from_job_description(cls, job_details, model_channel_name=None):
        """Convert the job description to init params that can be handled by the class constructor.

        Args:
            job_details: the returned job details from a describe_training_job
                API call.
            model_channel_name (str): Name of the channel where pre-trained
                model data will be downloaded.

        Returns:
            dictionary: The transformed init_params
        """
        init_params = super(PyTorch, cls)._prepare_init_params_from_job_description(
            job_details, model_channel_name
        )
        image_uri = init_params.pop("image_uri")
        framework, py_version, tag, _ = framework_name_from_image(image_uri)
        if framework:
            framework = framework.split("-")[0]

        if tag is None:
            framework_version = None
        else:
            framework_version = framework_version_from_tag(tag)
        init_params["framework_version"] = framework_version
        init_params["py_version"] = py_version

        if not framework:
            # If we were unable to parse the framework name from the image it is not one of our
            # officially supported images, in this case just add the image to the init params.
            init_params["image_uri"] = image_uri
            return init_params

        if framework != cls._framework_name:
            raise ValueError(
                "Training job: {} didn't use image for requested framework".format(
                    job_details["TrainingJobName"]
                )
            )

        return init_params

    @classmethod
    def _setup_for_training_recipe(cls, training_recipe, recipe_overrides, kwargs):
        """Performs training recipe specific setup and returns recipe specific args.

        Updates kwargs and returns a dictionary of args to use for estimator
        initialization and setup when using a training recipe. Updates the paths in
        the recipe for Sagemaker Jobs environment.

        Args:
            training_recipe (str): A recipe which is a local file path, a url or a
                                   sagemaker training recipe.
            recipe_overrides (Dict): Dictionary specifying key values to override in the
                                     training_recipe.
            kwargs (dict): Dictionary of args used for estimator initializaiton.
        Returns:
            dict containing arg values for estimator initialization and setup.

        """
        if recipe_overrides is None:
            recipe_overrides = dict()
        cls.recipe_train_dir = tempfile.TemporaryDirectory(prefix="training_")
        cls.recipe_launcher_dir = tempfile.TemporaryDirectory(prefix="launcher_")

        temp_local_recipe = tempfile.NamedTemporaryFile(prefix="recipe").name
        if training_recipe.endswith(".yaml"):
            if os.path.isfile(training_recipe):
                shutil.copy(training_recipe, temp_local_recipe)
            else:
                try:
                    urlretrieve(training_recipe, temp_local_recipe)
                except Exception as e:
                    raise ValueError(
                        f"Could not fetch the provided recipe {training_recipe}: exception {str(e)}"
                    )
        else:
            launcher_repo = os.environ.get("training_launcher_git", None) or cls.SM_LAUNCHER_REPO
            _run_clone_command(launcher_repo, cls.recipe_launcher_dir.name)
            recipe = os.path.join(
                cls.recipe_launcher_dir.name,
                "recipes-collection",
                "recipes",
                "training",
                training_recipe + ".yaml",
            )
            if os.path.isfile(recipe):
                shutil.copy(recipe, temp_local_recipe)
            else:
                raise ValueError(f"Recipe {training_recipe} not found.")

        recipe = OmegaConf.load(temp_local_recipe)

        if "instance_type" not in kwargs:
            raise ValueError("Must pass instance type to estimator when using training recipes.")
        instance_type = kwargs["instance_type"].split(".")[1]
        if instance_type.startswith(("p", "g")):
            device_type = "gpu"
        elif instance_type.startswith("trn"):
            device_type = "trainium"
        else:
            device_type = "cpu"

        if "trainer" not in recipe:
            raise ValueError("Supplied recipe does not contain required field trainer.")
        if "instance_count" in kwargs and "num_nodes" in recipe["trainer"]:
            logger.warning(
                "Using instance_count argument to estimator to set number "
                " of nodes. Ignoring trainer -> num_nodes in recipe."
            )
        if "instance_count" not in kwargs:
            if "num_nodes" not in recipe["trainer"]:
                raise ValueError(
                    "Must set either instance_count argument for estimator or"
                    "set trainer -> num_nodes in recipe."
                )
            kwargs["instance_count"] = recipe["trainer"]["num_nodes"]

        args = dict()
        # [TODO] Add image uris to image_uri_config/_.json and use image_uris.retrieve
        # to retrieve the image uri below before we go GA.
        if device_type == "gpu":
            adapter_repo = os.environ.get("training_adapter_git", None) or cls.SM_ADAPTER_REPO
            _run_clone_command(adapter_repo, cls.recipe_train_dir.name)

            model_type_to_entry = {
                "llama_v3": ("llama", "llama_pretrain.py"),
                "mistral": ("mistral", "mistral_pretrain.py"),
                "mixtral": ("mixtral", "mixtral_pretrain.py"),
            }

            if "model" not in recipe:
                raise ValueError("Supplied recipe does not contain required field model.")
            if "model_type" not in recipe["model"]:
                raise ValueError("Supplied recipe does not contain required field model_type.")
            model_type = recipe["model"]["model_type"]
            if model_type not in model_type_to_entry:
                raise ValueError(f"Model type {model_type} not supported")

            args["source_dir"] = os.path.join(
                cls.recipe_train_dir.name, "examples", model_type_to_entry[model_type][0]
            )
            args["entry_point"] = model_type_to_entry[model_type][1]
            args["default_image_uri"] = cls.SM_TRAINING_RECIPE_GPU_IMG
            smp_options = {
                "enabled": True,
                "parameters": {
                    "placement_strategy": "cluster",
                },
            }
            args["distribution"] = {
                "smdistributed": {"modelparallel": smp_options},
                "torch_distributed": {"enabled": True},
            }
        elif device_type == "trainium":
            _run_clone_command(cls.SM_NEURONX_DIST_REPO, cls.recipe_train_dir.name)
            args["source_dir"] = os.path.join(cls.recipe_train_dir.name, "examples")
            args["entry_point"] = "training_orchestrator.py"
            args["default_image_uri"] = cls.SM_NEURONX_DIST_IMG
            args["distribution"] = {
                "torch_distributed": {"enabled": True},
            }
        else:
            raise ValueError(
                f"Devices of type {device_type} are not supported with training recipes."
            )

        recipe_overrides.setdefault("run", dict())["results_dir"] = "/opt/ml/model"
        recipe_overrides.setdefault("exp_manager", dict())["exp_dir"] = "/opt/ml/model/"
        recipe = OmegaConf.merge(recipe, recipe_overrides)

        if "container" in recipe and not recipe["container"]:
            logger.warning(
                "Ignoring container from training_recipe. Use image_uri arg for estimator."
            )

        OmegaConf.save(config=recipe, f=os.path.join(args["source_dir"], "recipe.yaml"))
        args["hyperparameters"] = {"config-path": ".", "config-name": "recipe.yaml"}

        return args
