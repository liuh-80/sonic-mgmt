import logging
import pytest
from pytest_ansible.errors import AnsibleConnectionFailure
from tests.tacacs.utils import setup_tacacs_client, setup_tacacs_server, load_tacacs_creds,\
                    cleanup_tacacs, restore_tacacs_servers, print_tacacs_creds

logger = logging.getLogger(__name__)
TACACS_CREDS_FILE = 'tacacs_creds.yaml'

# Following UT not enable TACACS test:


def drop_all_ssh_session(duthost):
    try:
        duthost.shell('sudo ss -K sport ssh')
    except AnsibleConnectionFailure:
        # Current connection will throw AnsibleConnectionFailure after connection broken
        pass


@pytest.fixture(scope="module")
def tacacs_creds(creds_all_duts):
    hardcoded_creds = load_tacacs_creds()
    creds_all_duts.update(hardcoded_creds)
    return creds_all_duts


def collect_artifact(duthost, module_name):
    files = ["/etc/passwd", "/var/log/auth.log", "/var/log/syslog"]
    dst_patch = "logs/tacacs/{}".format(module_name)
    for file in files:
        duthost.fetch(src=file, dest=dst_patch, flat=True)


@pytest.fixture(scope="module", autouse=True)
def setup_tacacs(ptfhost, duthosts, selected_dut, tacacs_creds, creds, request):
    print_tacacs_creds(tacacs_creds)

    # setup_tacacs only support test case using duthost
    if not selected_dut:
        logger.debug("Ignore setup_tacacs because can't find duthost for test.")
        yield
        return

    duthost = duthosts[selected_dut]

    # Setup TACACS config according to flag defined in /ansible/group_vars/lab/lab.yml
    use_lab_tacacs_server = creds['test_with_lab_tacacs_server']
    tacacs_server_ip = ""
    tacacs_server_passkey = ""
    if use_lab_tacacs_server:
        tacacs_server_ip = creds['lab_tacacs_server']
        tacacs_server_passkey = creds['lab_tacacs_passkey']
    else:
        tacacs_server_ip = ptfhost.mgmt_ip
        tacacs_server_passkey = tacacs_creds[duthost.hostname]['tacacs_passkey']

    logger.debug("Setup TACACS server: use_lab_tacacs_server:{}, tacacs_server_ip:{}, tacacs_server_passkey:{}"
                 .format(use_lab_tacacs_server, tacacs_server_ip, tacacs_server_passkey))

    # setup per-command authorization with 'tacacs+ local', when command blocked by TACACS server, UT still can pass.
    setup_tacacs_client(duthost, tacacs_creds, tacacs_server_ip, tacacs_server_passkey, "\"tacacs+ local\"")

    if not use_lab_tacacs_server:
        setup_tacacs_server(ptfhost, tacacs_creds, duthost)

    # After TACACS enabled, only new ssh session control by TACACS.
    # Ansible use connection pool, existing connection in pool not control by TACACAS
    # Drop all connection to force ansible re-connect.
    drop_all_ssh_session(duthost)

    yield

    if request.session.testsfailed:
        collect_artifact(duthost, request.module.__name__)

    cleanup_tacacs(ptfhost, tacacs_creds, duthost)

    if not use_lab_tacacs_server:
        restore_tacacs_servers(duthost)
