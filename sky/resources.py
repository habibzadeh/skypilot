"""Resources: compute requirements of Tasks."""
from typing import Dict, List, Optional, Union

from sky import clouds
from sky import sky_logging

logger = sky_logging.init_logger(__name__)

_DEFAULT_DISK_SIZE_GB = 256


class Resources:
    """A cloud resource bundle.

    Used
      * for representing resource requests for tasks/apps
      * as a "filter" to get concrete launchable instances
      * for calculating billing
      * for provisioning on a cloud

    Examples:

        # Fully specified cloud and instance type (is_launchable() is True).
        sky.Resources(clouds.AWS(), 'p3.2xlarge')
        sky.Resources(clouds.GCP(), 'n1-standard-16')
        sky.Resources(clouds.GCP(), 'n1-standard-8', 'V100')

        # Specifying required resources; Sky decides the cloud/instance type.
        # The below are equivalent:
        sky.Resources(accelerators='V100')
        sky.Resources(accelerators='V100:1')
        sky.Resources(accelerators={'V100': 1})

        # TODO:
        sky.Resources(requests={'mem': '16g', 'cpu': 8})
    """

    def __init__(
        self,
        cloud: Optional[clouds.Cloud] = None,
        instance_type: Optional[str] = None,
        accelerators: Union[None, str, Dict[str, int]] = None,
        accelerator_args: Optional[Dict[str, str]] = None,
        use_spot: Optional[bool] = None,
        disk_size: Optional[int] = None,
        ips: Optional[List[str]] = None,
    ):
        self.cloud = cloud

        # Check for Local Config
        if cloud is not None and isinstance(self.cloud, clouds.Local):
            assert instance_type is None and use_spot is None and \
            disk_size is None and ips, 'Resources are passed incorrectly ' + \
            'to Local/On-Prem.'

        self.instance_type = instance_type
        assert not (instance_type is not None and cloud is None), \
            'If instance_type is specified, must specify the cloud'
        if accelerators is not None:
            if isinstance(accelerators, str):  # Convert to Dict[str, int].
                if ':' not in accelerators:
                    accelerators = {accelerators: 1}
                else:
                    splits = accelerators.split(':')
                    parse_error = ('The "accelerators" field as a str '
                                   'should be <name> or <name>:<cnt>. '
                                   f'Found: {accelerators!r}')
                    if len(splits) != 2:
                        raise ValueError(parse_error)
                    try:
                        accelerators = {splits[0]: int(splits[1])}
                    except ValueError:
                        try:
                            accelerators = {splits[0]: float(splits[1])}
                        except ValueError:
                            raise ValueError(parse_error) from None
            assert len(accelerators) == 1, accelerators

            acc, _ = list(accelerators.items())[0]
            if 'tpu' in acc.lower():
                if cloud is None:
                    cloud = clouds.GCP()
                assert cloud.is_same_cloud(clouds.GCP()), 'Cloud must be GCP.'
                if accelerator_args is None:
                    accelerator_args = {}
                if 'tf_version' not in accelerator_args:
                    logger.info('Missing tf_version in accelerator_args, using'
                                ' default (2.5.0)')
                    accelerator_args['tf_version'] = '2.5.0'

        self._accelerators = accelerators
        self.accelerator_args = accelerator_args

        self._use_spot_specified = use_spot is not None
        self.use_spot = use_spot if use_spot is not None else False

        if disk_size is not None:
            if disk_size < 50:
                raise ValueError(
                    f'OS disk size must be larger than 50GB. Got: {disk_size}.')
            if round(disk_size) != disk_size:
                raise ValueError(
                    f'OS disk size must be an integer. Got: {disk_size}.')
            self.disk_size = int(disk_size)
        else:
            self.disk_size = _DEFAULT_DISK_SIZE_GB

        self.ips = ips

        self._try_validate_accelerators()

    def __repr__(self) -> str:
        accelerators = ''
        accelerator_args = ''
        if self.accelerators is not None:
            accelerators = f', {self.accelerators}'
            if self.accelerator_args is not None:
                accelerator_args = f', accelerator_args={self.accelerator_args}'
        use_spot = ''
        if self.use_spot:
            use_spot = '[Spot]'
        return (f'{self.cloud}({self.instance_type}{use_spot}'
                f'{accelerators}{accelerator_args})')

    def is_launchable(self) -> bool:
        return self.cloud is not None and self.instance_type is not None

    def _try_validate_accelerators(self) -> None:
        """Try-validates accelerators against the instance type."""
        if self.is_launchable() and not isinstance(self.cloud, clouds.GCP):
            # GCP attaches accelerators to VMs, so no need for this check.
            acc_requested = self.accelerators
            if acc_requested is None:
                return
            acc_from_instance_type = (
                self.cloud.get_accelerators_from_instance_type(
                    self.instance_type))
            if not Resources(accelerators=acc_requested).less_demanding_than(
                    Resources(accelerators=acc_from_instance_type)):
                raise ValueError(
                    'Infeasible resource demands found:\n'
                    f'  Instance type requested: {self.instance_type}\n'
                    f'  Accelerators for {self.instance_type}: '
                    f'{acc_from_instance_type}\n'
                    f'  Accelerators requested: {acc_requested}\n'
                    f'To fix: either only specify instance_type, or change '
                    'the accelerators field to be consistent.')
            # NOTE: should not clear 'self.accelerators' even for AWS/Azure,
            # because e.g., the instance may have 4 GPUs, while the task
            # specifies to use 1 GPU.

    @property
    def accelerators(self) -> Optional[Dict[str, int]]:
        """Returns the accelerators field directly or by inferring.

        For example, Resources(AWS, 'p3.2xlarge') has its accelerators field
        set to None, but this function will infer {'V100': 1} from the instance
        type.
        """
        if self._accelerators is not None:
            return self._accelerators
        if self.cloud is not None and self.instance_type is not None:
            return self.cloud.get_accelerators_from_instance_type(
                self.instance_type)
        return None

    @accelerators.setter
    def accelerators(self, accelerators: Union[None, Dict[str, int]]) -> None:
        self._accelerators = accelerators

    def get_cost(self, seconds: float):
        """Returns cost in USD for the runtime in seconds."""
        hours = seconds / 3600
        # Instance.
        hourly_cost = self.cloud.instance_type_to_hourly_cost(
            self.instance_type, self.use_spot)
        # Accelerators (if any).
        if self.accelerators is not None:
            hourly_cost += self.cloud.accelerators_to_hourly_cost(
                self.accelerators)
        return hourly_cost * hours

    def is_same_resources(self, other: 'Resources') -> bool:
        """Returns whether two resources are the same.

        Returns True if they are the same, False if not.
        """
        if (self.cloud is None) != (other.cloud is None):
            # self and other's cloud should be both None or both not None
            return False

        if self.cloud is not None and not self.cloud.is_same_cloud(other.cloud):
            return False
        # self.cloud == other.cloud

        if (self.instance_type is not None and
                self.instance_type != other.instance_type):
            return False
        # self.instance_type == other.instance_type

        other_accelerators = other.accelerators
        accelerators = self.accelerators
        if accelerators != other_accelerators:
            return False
        # self.accelerators == other.accelerators

        if self.accelerator_args != other.accelerator_args:
            return False
        # self.accelerator_args == other.accelerator_args

        if self.use_spot != other.use_spot:
            return False

        # self == other
        return True

    def less_demanding_than(self, other: 'Resources') -> bool:
        """Returns whether this resources is less demanding than the other."""

        # Local case, assume true
        if isinstance(self.cloud, clouds.Local) or isinstance(
                other.cloud, clouds.Local):
            return True
        if self.cloud is not None and not self.cloud.is_same_cloud(other.cloud):
            return False
        # self.cloud <= other.cloud

        if (self.instance_type is not None and
                self.instance_type != other.instance_type):
            return False
        # self.instance_type <= other.instance_type

        other_accelerators = other.accelerators
        if self.accelerators is not None and other_accelerators is None:
            return False

        if self.accelerators is not None:
            for acc in self.accelerators:
                if acc not in other_accelerators:
                    return False
                if self.accelerators[acc] > other_accelerators[acc]:
                    return False
        # self.accelerators <= other.accelerators

        if (self.accelerator_args is not None and
                self.accelerator_args != other.accelerator_args):
            return False
        # self.accelerator_args == other.accelerator_args

        if self.use_spot != other.use_spot:
            return False

        # self <= other
        return True

    def is_launchable_fuzzy_equal(self, other: 'Resources') -> bool:
        """Whether the resources are the fuzzily same launchable resources."""
        assert self.cloud is not None and other.cloud is not None
        if not self.cloud.is_same_cloud(other.cloud):
            return False
        if self.instance_type is not None or other.instance_type is not None:
            return self.instance_type == other.instance_type
        # For GCP, when a accelerator type fails to launch, it should be blocked
        # regardless of the count, since the larger number will fail either.
        return (self.accelerators is None and other.accelerators is None
               ) or self.accelerators.keys() == other.accelerators.keys()

    def is_empty(self) -> bool:
        """Is this Resources an empty request (all fields None)?"""
        return all([
            self.cloud is None,
            self.instance_type is None,
            self.accelerators is None,
            self.accelerator_args is None,
            not self._use_spot_specified,
        ])
