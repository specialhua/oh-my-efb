from unittest import mock

import pytest
from telegram.error import TimedOut

from ehforwarderbot import coordinator
from efb_telegram_master.message import ETMMsg


def test_failed_file_download_is_not_retried_on_later_access():
    bot_manager = mock.Mock()
    bot_manager.get_file.side_effect = TimedOut()
    master = mock.Mock(bot_manager=bot_manager)

    msg = ETMMsg()
    msg.file_id = "file-id"
    with mock.patch.object(coordinator, "master", master, create=True):
        with pytest.raises(TimedOut):
            _ = msg.file
        # Cleanup after a failed send reads these again; it must not download again.
        assert msg.file is None
        assert msg.path is None
    assert bot_manager.get_file.call_count == 1
