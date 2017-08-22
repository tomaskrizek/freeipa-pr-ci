# FreeIPA Pull Request CI

Scripts that are used by the PR CI testing infrastructure.

## Contributor's Guide

If you're looking for an overview of what the system looks like for
contributors, you can check out this [video](https://vimeo.com/228077191).

## Setting up runner

### Prerequisites

#### Runner

- **Dedicated machine**: runners have some configuration and behaviour that's
  not desirable for a workstation. Virtual machine can be used if the
  hypervisor has support for nested virtualization.
- **Root access**: set up root access to the machine. This can be done
  with [ssh_config](https://linux.die.net/man/5/ssh_config),
  [ansible inventory parameters](https://docs.ansible.com/ansible/latest/intro_inventory.html#list-of-behavioral-inventory-parameters)
  or [ansible configuration](https://docs.ansible.com/ansible/latest/intro_configuration.html).
  (*Note*: please use `freeipa_pr_ci` key for root access if you have it)
- **Hostname**: short hostname is used as the runner identifier, please make
  sure it is unique.
- **Persistent configuration**: runner may be rebooted occassionally. All
  configuration including hostname and networking should persist after reboot.
- **Minimum specs**: 4 vCPUs, 8 GB RAM, 30 GB disk.
- **Operating system**: currently, only Fedora is supported.

#### Other

- `ansible >= 2.2` on the control machine
- GitHub token with *Full control of private repositories* (Settings ->
  Personal access tokens)
- `freeipa_pr_ci` private key placed in `keys/`

### Runner deployment

1. Create ansible inventory `ansible/hosts/runners`

   ```
   [runners]
   1.2.3.4
   ```

2. Run the `prepare_test_runners.yml` playbook

   ```
   ansible-playbook \
     -i ansible/hosts/runners \
     ansible/prepare_test_runners.yml
   ```

This will set up the runner machine with `prci` service, which is configured
to start at boot. The runner is periodically updated to use the latest code.
In the unfortunate event the update didn't execute successfully, the service
will enter a failed state. Re-running the above `ansible-playbook` command
from the up-to-date code should fix such issues.

#### Monitoring runner activity

```bash
systemctl status prci
journalctl -fu prci
```

### Note on runner deployment automation

By default, ansible will prompt for API token and other variables. To fully
automate the deployment, you can pass the variables as arguments to the
`ansible-playbook` command with `-e key=value`. To find the variable names,
check the ansible playbooks.

### Setting up development runner

The development runner can be deployed to monitor any FreeIPA fork while
executing any FreeIPA PR CI fork (any owner and branch). This is useful for
testing and development purposes.

1. Create ansible inventory `ansible/hosts/runners`

   ```
   [runners_devel]
   2.3.4.5
   ```

2. Run the `prepare_devel_test_runners.yml` playbook, specify which repository
   should be monitored and which code repository and branch should be used to
   deploy the runner

   ```
   ansible-playbook \
     -i ansible/hosts/runners
     ansible/prepare_devel_test_runners.yml
   ```

## Creating vagrant template box

1. Make sure correct variables for Fedora version are selected in
   `ansible/create_box_template.yml`.

2. Run `create-box-template`

   This will create a vagrant box in `/tmp/$box_name/`. It will also be
   published on vagrantcloud if you have credentials set up. (**TODO**)
