import logging
import os

from . import constants
from .common import PopenTask, PopenException, FallibleTask, TaskException


def with_vagrant(func):
    def wrapper(self, *args, **kwargs):
        try:
            __setup_provision(self)
        except TaskException as exc:
            logging.critical('vagrant or provisioning failed')
            raise exc
        else:
            func(self, *args, **kwargs)
        finally:
            if not self.no_destroy:
                self.execute_subtask(
                    VagrantCleanup(raise_on_err=False))

    return wrapper


def __setup_provision(task):
    """
    This tries to execute the provision twice due to
    problems described in issue #20
    """
    task.execute_subtask(
        VagrantBoxDownload(
            box_name=task.template_name,
            box_version=task.template_version,
            link_image=task.link_image,
            timeout=None))
    try:
        task.execute_subtask(VagrantUp(timeout=None))
        task.execute_subtask(VagrantProvision(timeout=None))
    except Exception as exc:
        logging.debug(exc, exc_info=True)
        logging.info("Failed to provision/up VM. Trying it again")
        task.execute_subtask(VagrantCleanup(raise_on_err=False))
        task.execute_subtask(VagrantUp(timeout=None))
        task.execute_subtask(VagrantProvision(timeout=None))


class VagrantTask(FallibleTask):
    def __init__(self, **kwargs):
        super(VagrantTask, self).__init__(**kwargs)
        self.timeout = kwargs.get('timeout', None)


class VagrantUp(VagrantTask):
    def _run(self):
        self.execute_subtask(
            PopenTask(['vagrant', 'up', '--no-provision', '--parallel'],
                      timeout=None))


class VagrantProvision(VagrantTask):
    def _run(self):
        self.execute_subtask(
            PopenTask(['vagrant', 'provision'], timeout=None))


class VagrantCleanup(VagrantTask):
    def _run(self):
        try:
            self.execute_subtask(
                PopenTask(['vagrant', 'destroy']))
        except PopenException:
            self.execute_subtask(
                PopenTask(['pkill', '-9', 'bin/vagrant'],
                          raise_on_err=False))
            self.execute_subtask(
                PopenTask(['systemctl', 'restart', 'libvirt'],
                          raise_on_err=False))
            self.execute_subtask(
                PopenTask(['vagrant', 'destroy'],
                          raise_on_err=False))


class VagrantBoxDownload(VagrantTask):
    def __init__(self, box_name, box_version, link_image=True, **kwargs):
        """
        link_image: if True, a symbolic link will be created in libvirt to
                    conserve storage (otherwise, libvirt copies it by default)
        """
        super(VagrantBoxDownload, self).__init__(**kwargs)
        self.box = VagrantBox(box_name, box_version)
        self.link_image = True

    def _run(self):
        if not self.box.exists():
            try:
                self.execute_subtask(
                    PopenTask([
                        'vagrant', 'box', 'add', self.box.name,
                        '--box-version', self.box.version,
                        '--provider', self.box.provider],
                        timeout=None))
            except TaskException as exc:
                logging.error('Box download failed')
                raise exc

        # link box to libvirt
        if self.link_image and not self.box.libvirt_exists():
            try:
                self.execute_subtask(
                    PopenTask([
                        'ln', self.box.vagrant_path, self.box.libvirt_path]))
                self.execute_subtask(
                    PopenTask([
                        'chown', 'qemu:qemu', self.box.libvirt_path]))
                self.execute_subtask(
                    PopenTask(['virsh', 'pool-refresh', 'default']))
            except TaskException as exc:
                logging.warning('Failed to create libvirt link to image')
                raise exc


class VagrantBox(object):
    def __init__(self, name, version, provider="libvirt"):
        self.name = name
        self.version = version
        self.provider = provider

    @property
    def escaped_name(self):
        return self.name.replace(
            '/', '-VAGRANTSLASH-')

    @property
    def vagrant_path(self):
        return constants.VAGRANT_IMAGE_PATH.format(
            name=self.escaped_name,
            version=self.version,
            provider=self.provider)

    @property
    def libvirt_name(self):
        return '{escaped_name}_vagrant_box_image'.format(
            escaped_name=self.escaped_name)

    @property
    def libvirt_path(self):
        return constants.LIBVIRT_IMAGE_PATH.format(
            libvirt_name=self.libvirt_name,
            version=self.version)

    def exists(self):
        return os.path.exists(self.vagrant_path)

    def libvirt_exists(self):
        return os.path.exists(self.libvirt_path)
