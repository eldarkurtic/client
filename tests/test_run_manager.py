import datetime
import io
import os
import time
import pytest
import threading

import wandb.run_manager
import wandb
from wandb import wandb_socket
from wandb.apis import internal
from wandb.wandb_run import Run, RESUME_FNAME
from wandb.run_manager import FileEventHandlerThrottledOverwrite, FileEventHandlerOverwriteDeferred
from click.testing import CliRunner


def test_check_update_available_equal(request_mocker, capsys):
    "Test update availability in different cases."
    test_cases = [
        ('0.8.10', '0.8.10', False),
        ('0.8.9', '0.8.10', True),
        ('0.8.11', '0.8.10', False),
        ('1.0.0', '2.0.0', True),
        ('0.4.5', '0.4.5a5', False),
        ('0.4.5', '0.4.3b2', False),
        ('0.4.5', '0.4.6b2', True),
        ('0.4.5.alpha', '0.4.4', False),
        ('0.4.5.alpha', '0.4.5', True),
        ('0.4.5.alpha', '0.4.6', True)
    ]

    for current, latest, is_expected in test_cases:
        with CliRunner().isolated_filesystem():
            is_avail = _is_update_avail(
                request_mocker, capsys, current, latest)
            assert is_avail == is_expected, "expected %s compared to %s to yield update availability of %s" % (
                current, latest, is_expected)


def _is_update_avail(request_mocker, capsys, current, latest):
    "Set up the run manager and detect if the upgrade message is printed."
    api = internal.Api(
        load_settings=False,
        retry_timedelta=datetime.timedelta(0, 0, 50))
    api.set_current_run_id(123)
    run = Run()
    run_manager = wandb.run_manager.RunManager(api, run)

    # Without this mocking, during other tests, the _check_update_available
    # function will throw a "mock not found" error, then silently fail without
    # output (just like it would in a normal network failure).
    response = b'{ "info": { "version": "%s" } }' % bytearray(latest, 'utf-8')
    request_mocker.register_uri('GET', 'https://pypi.org/pypi/wandb/json',
                                content=response, status_code=200)
    run_manager._check_update_available(current)

    captured_out, captured_err = capsys.readouterr()
    print(captured_out, captured_err)
    return "To upgrade, please run:" in captured_err


@pytest.fixture
def run_manager(mocker):
    """This fixture emulates the run_manager headless mode in a single process
    Just call run_manager.test_shutdown() to join the threads
    """
    api = internal.Api(load_settings=False)
    with CliRunner().isolated_filesystem():
        wandb.run = Run()
        wandb.run.socket = wandb_socket.Server()
        api.set_current_run_id(wandb.run.id)
        api._file_stream_api = mocker.MagicMock()
        run_manager = wandb.run_manager.RunManager(
            api, wandb.run, port=wandb.run.socket.port)
        run_manager.proc = mocker.MagicMock()
        run_manager._stdout_tee = mocker.MagicMock()
        run_manager._stderr_tee = mocker.MagicMock()
        run_manager._output_log = mocker.MagicMock()
        run_manager._stdout_stream = mocker.MagicMock()
        run_manager._stderr_stream = mocker.MagicMock()
        socket_thread = threading.Thread(
            target=wandb.run.socket.listen)
        socket_thread.start()
        run_manager._socket.ready()
        thread = threading.Thread(
            target=run_manager._sync_etc)
        thread.start()

        def test_shutdown():
            wandb.run.socket.done()
            # TODO: is this needed?
            socket_thread.join()
            thread.join()
        run_manager.test_shutdown = test_shutdown
        run_manager._unblock_file_observer()
        run_manager._file_pusher._push_function = mocker.MagicMock()
        yield run_manager
        wandb.uninit()


def test_throttle_file_poller(mocker, run_manager):
    emitter = run_manager.emitter
    assert emitter.timeout == 1
    for i in range(100):
        with open(os.path.join(wandb.run.dir, "file_%i.txt" % i), "w") as f:
            f.write(str(i))
    run_manager.test_shutdown()
    assert emitter.timeout == 2


def test_custom_file_policy(mocker, run_manager):
    for i in range(5):
        with open(os.path.join(wandb.run.dir, "ckpt_%i.txt" % i), "w") as f:
            f.write(str(i))
    wandb.save("ckpt*")

    run_manager.test_shutdown()
    assert isinstance(
        run_manager._file_event_handlers["ckpt_0.txt"], FileEventHandlerThrottledOverwrite)
    assert isinstance(
        run_manager._file_event_handlers["wandb-metadata.json"], FileEventHandlerOverwriteDeferred)


def test_custom_file_policy_symlink(mocker, run_manager):
    mod = mocker.MagicMock()
    mocker.patch(
        'wandb.run_manager.FileEventHandlerThrottledOverwrite.on_modified', mod)
    with open("ckpt_0.txt", "w") as f:
        f.write("joy")
    with open("ckpt_1.txt", "w") as f:
        f.write("joy" * 100)
    wandb.save("ckpt_0.txt")
    with open("ckpt_0.txt", "w") as f:
        f.write("joy" * 100)
    wandb.save("ckpt_1.txt")
    run_manager.test_shutdown()
    assert isinstance(
        run_manager._file_event_handlers["ckpt_0.txt"], FileEventHandlerThrottledOverwrite)
    assert mod.called


def test_remove_auto_resume(mocker, run_manager):
    resume_path = os.path.join(wandb.wandb_dir(), RESUME_FNAME)
    with open(resume_path, "w") as f:
        f.write("{}")
    run_manager.test_shutdown()
    assert not os.path.exists(resume_path)
