from typing import Dict

from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.core import CfnTag, Construct, Fn, NestedStack, Stack

from pcluster.config.cluster_config import LoginNodesPool, SharedStorageType, SlurmClusterConfig
from pcluster.constants import NODE_BOOTSTRAP_TIMEOUT, OS_MAPPING, PCLUSTER_LOGIN_NODES_POOL_NAME_TAG
from pcluster.templates.cdk_builder_utils import (
    CdkLaunchTemplateBuilder,
    get_common_user_data_env,
    get_default_instance_tags,
    get_default_volume_tags,
    get_login_nodes_security_groups_full,
    get_shared_storage_ids_by_type,
    get_user_data_content,
    to_comma_separated_string,
)
from pcluster.utils import get_attr, get_http_tokens_setting


class Pool(Construct):
    """Construct defining Login Nodes Pool specific resources."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        pool: LoginNodesPool,
        config: SlurmClusterConfig,
        shared_storage_infos,
        shared_storage_mount_dirs: Dict,
        shared_storage_attributes: Dict,
        login_security_group,
        stack_name,
    ):
        super().__init__(scope, id)
        self._pool = pool
        self._config = config
        self._login_nodes_stack_id = id
        self._shared_storage_infos = shared_storage_infos
        self._shared_storage_mount_dirs = shared_storage_mount_dirs
        self._shared_storage_attributes = shared_storage_attributes
        self._login_security_group = login_security_group
        self.stack_name = stack_name

        self._add_resources()

    def _add_resources(self):
        self._vpc = ec2.Vpc.from_vpc_attributes(
            self,
            f"VPC{self._pool.name}",
            vpc_id=self._config.vpc_id,
            availability_zones=self._pool.networking.az_list,
        )
        self._login_nodes_pool_target_group = self._add_login_nodes_pool_target_group()
        self._login_nodes_pool_load_balancer = self._add_login_nodes_pool_load_balancer(
            self._login_nodes_pool_target_group
        )

        self._launch_template = self._add_login_nodes_pool_launch_template()
        self._add_login_nodes_pool_auto_scaling_group()

    def _add_login_nodes_pool_launch_template(self):
        login_nodes_pool_lt_security_groups = get_login_nodes_security_groups_full(
            self._login_security_group,
            self._pool,
        )
        login_nodes_pool_lt_nw_interface = [
            ec2.CfnLaunchTemplate.NetworkInterfaceProperty(
                device_index=0,
                interface_type=None,
                groups=login_nodes_pool_lt_security_groups,
                subnet_id=self._pool.networking.subnet_ids[0],
            )
        ]
        return ec2.CfnLaunchTemplate(
            self,
            f"LoginNodeLaunchTemplate{self._pool.name}",
            launch_template_name=f"{self.stack_name}-{self._pool.name}",
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                image_id=self._config.login_nodes_ami[self._pool.name],
                instance_type=self._pool.instance_type,
                key_name=self._pool.ssh.key_name,
                metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                    http_tokens=get_http_tokens_setting(self._config.imds.imds_support)
                ),
                user_data=Fn.base64(
                    Fn.sub(
                        get_user_data_content("../resources/login_node/user_data.sh"),
                        {
                            **{
                                # "EnableEfa": "efa" if compute_resource.efa and compute_resource.efa.enabled else "NONE",
                                "RAIDSharedDir": to_comma_separated_string(
                                    self._shared_storage_mount_dirs[SharedStorageType.RAID]
                                ),
                                "RAIDType": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.RAID]["Type"]
                                ),
                                "DisableMultiThreadingManually": "false",
                                # if compute_resource.disable_simultaneous_multithreading_manually
                                # else "false",
                                "BaseOS": self._config.image.os,
                                "EFSIds": get_shared_storage_ids_by_type(
                                    self._shared_storage_infos, SharedStorageType.EFS
                                ),
                                "EFSSharedDirs": to_comma_separated_string(
                                    self._shared_storage_mount_dirs[SharedStorageType.EFS]
                                ),
                                "EFSEncryptionInTransits": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.EFS]["EncryptionInTransits"],
                                    use_lower_case=True,
                                ),
                                "EFSIamAuthorizations": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.EFS]["IamAuthorizations"],
                                    use_lower_case=True,
                                ),
                                "FSXIds": get_shared_storage_ids_by_type(
                                    self._shared_storage_infos, SharedStorageType.FSX
                                ),
                                "FSXMountNames": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.FSX]["MountNames"]
                                ),
                                "FSXDNSNames": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.FSX]["DNSNames"]
                                ),
                                "FSXVolumeJunctionPaths": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.FSX]["VolumeJunctionPaths"]
                                ),
                                "FSXFileSystemTypes": to_comma_separated_string(
                                    self._shared_storage_attributes[SharedStorageType.FSX]["FileSystemTypes"]
                                ),
                                "FSXSharedDirs": to_comma_separated_string(
                                    self._shared_storage_mount_dirs[SharedStorageType.FSX]
                                ),
                                "Scheduler": self._config.scheduling.scheduler,
                                # "EphemeralDir": queue.compute_settings.local_storage.ephemeral_volume.mount_dir
                                # if isinstance(queue, SlurmQueue)
                                #    and queue.compute_settings.local_storage.ephemeral_volume
                                # else DEFAULT_EPHEMERAL_DIR,
                                "EbsSharedDirs": to_comma_separated_string(
                                    self._shared_storage_mount_dirs[SharedStorageType.EBS]
                                ),
                                "ClusterDNSDomain": str(self._cluster_hosted_zone.name)
                                if self._cluster_hosted_zone
                                else "",
                                "ClusterHostedZone": str(self._cluster_hosted_zone.ref)
                                if self._cluster_hosted_zone
                                else "",
                                "OSUser": OS_MAPPING[self._config.image.os]["user"],
                                "ClusterName": self.stack_name,
                                "SlurmDynamoDBTable": self._dynamodb_table.ref if self._dynamodb_table else "NONE",
                                "LogGroupName": self._log_group.log_group_name
                                if self._config.monitoring.logs.cloud_watch.enabled
                                else "NONE",
                                "IntelHPCPlatform": "true" if self._config.is_intel_hpc_platform_enabled else "false",
                                "CWLoggingEnabled": "true" if self._config.is_cw_logging_enabled else "false",
                                "LogRotationEnabled": "true" if self._config.is_log_rotation_enabled else "false",
                                # "QueueName": queue.name,
                                # "ComputeResourceName": compute_resource.name,
                                # "EnableEfaGdr": "compute"
                                # if compute_resource.efa and compute_resource.efa.gdr_support
                                # else "NONE",
                                "CustomNodePackage": self._config.custom_node_package or "",
                                "CustomAwsBatchCliPackage": self._config.custom_aws_batch_cli_package or "",
                                "ExtraJson": self._config.extra_chef_attributes,
                                "UsePrivateHostname": str(
                                    get_attr(self._config, "scheduling.settings.dns.use_ec2_hostnames", default=False)
                                ).lower(),
                                "HeadNodePrivateIp": self._head_eni.attr_primary_private_ip_address,
                                "DirectoryServiceEnabled": str(self._config.directory_service is not None).lower(),
                                "Timeout": str(
                                    get_attr(
                                        self._config,
                                        "dev_settings.timeouts.compute_node_bootstrap_timeout",
                                        NODE_BOOTSTRAP_TIMEOUT,
                                    )
                                ),
                                "ComputeStartupTimeMetricEnabled": str(
                                    get_attr(
                                        self._config,
                                        "dev_settings.compute_startup_time_metric_enabled",
                                        default=False,
                                    )
                                ),
                            },
                            # **get_common_user_data_env(queue, self._config),
                        },
                    )
                ),
                network_interfaces=login_nodes_pool_lt_nw_interface,
                tag_specifications=[
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="instance",
                        tags=get_default_instance_tags(
                            self.stack_name, self._config, self._pool, "LoginNode", self._shared_storage_infos
                        )
                        + [CfnTag(key=PCLUSTER_LOGIN_NODES_POOL_NAME_TAG, value=self._pool.name)],
                    ),
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type="volume",
                        tags=get_default_volume_tags(self.stack_name, "LoginNode")
                        + [CfnTag(key=PCLUSTER_LOGIN_NODES_POOL_NAME_TAG, value=self._pool.name)],
                    ),
                ],
            ),
        )

    def _add_login_nodes_pool_auto_scaling_group(self):
        launch_template_specification = autoscaling.CfnAutoScalingGroup.LaunchTemplateSpecificationProperty(
            launch_template_id=self._launch_template.ref,
            version=self._launch_template.attr_latest_version_number,
        )

        auto_scaling_group = autoscaling.CfnAutoScalingGroup(
            self,
            f"{self._login_nodes_stack_id}-AutoScalingGroup",
            launch_template=launch_template_specification,
            min_size=str(self._pool.count),
            max_size=str(self._pool.count),
            desired_capacity=str(self._pool.count),
            target_group_arns=[self._login_nodes_pool_target_group.node.default_child.ref],
            vpc_zone_identifier=self._pool.networking.subnet_ids,
        )

        return auto_scaling_group

    def _add_login_nodes_pool_target_group(self):
        return elbv2.NetworkTargetGroup(
            self,
            f"{self._pool.name}TargetGroup",
            health_check=elbv2.HealthCheck(
                port="22",
                protocol=elbv2.Protocol.TCP,
            ),
            port=22,
            protocol=elbv2.Protocol.TCP,
            target_type=elbv2.TargetType.INSTANCE,
            vpc=self._vpc,
        )

    def _add_login_nodes_pool_load_balancer(
        self,
        target_group,
    ):
        login_nodes_load_balancer = elbv2.NetworkLoadBalancer(
            self,
            f"{self._pool.name}LoadBalancer",
            vpc=self._vpc,
            internet_facing=self._pool.networking.is_subnet_public,
            vpc_subnets=ec2.SubnetSelection(
                subnets=[
                    ec2.Subnet.from_subnet_id(self, f"LoginNodesSubnet{i}", subnet_id)
                    for i, subnet_id in enumerate(self._pool.networking.subnet_ids)
                ]
            ),
        )

        listener = login_nodes_load_balancer.add_listener(f"LoginNodesListener{self._pool.name}", port=22)
        listener.add_target_groups(f"LoginNodesListenerTargets{self._pool.name}", target_group)
        return login_nodes_load_balancer


class LoginNodesStack(NestedStack):
    """Stack encapsulating a set of LoginNodes and the associated resources."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        cluster_config: SlurmClusterConfig,
        shared_storage_infos: Dict,
        shared_storage_mount_dirs: Dict,
        shared_storage_attributes: Dict,
        login_security_group,
    ):
        super().__init__(scope, id)
        self._login_nodes = cluster_config.login_nodes
        self._config = cluster_config
        self._login_security_group = login_security_group
        self._launch_template_builder = CdkLaunchTemplateBuilder()
        self._shared_storage_infos = shared_storage_infos
        self._shared_storage_mount_dirs = shared_storage_mount_dirs
        self._shared_storage_attributes = shared_storage_attributes

        self._add_resources()

    @property
    def stack_name(self):
        """Name of the CFN stack."""
        return Stack.of(self.nested_stack_parent).stack_name

    def _add_resources(self):
        self.pools = {}
        for pool in self._login_nodes.pools:
            pool_construct = Pool(
                self,
                f"Pool{pool.name}",
                pool,
                self._config,
                self._shared_storage_infos,
                self._shared_storage_mount_dirs,
                self._shared_storage_attributes,
                self._login_security_group,
                self.stack_name,
            )
            self.pools[pool.name] = pool_construct
