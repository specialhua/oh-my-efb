from unittest import mock
from xml.etree import ElementTree as ETree

from efb_wechat_slave.slave_message import SlaveMessageManager


def test_slave_message_get_node_text():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <root>
        <item>
            <child>Text</child>
        </item>
        <emptyItem/>
    </root>
    """

    root = ETree.fromstring(xml)

    assert SlaveMessageManager.get_node_text(root, "./item/child", "") == "Text"
    assert SlaveMessageManager.get_node_text(root, "./item/non_existing", "fallback") == "fallback"
    assert SlaveMessageManager.get_node_text(root, "./emptyItem", "fallback") == "fallback"


def test_slave_message_channels_post():
    xml = ETree.fromstring("""<?xml version="1.0"?>
    <msg>
        <appmsg appid="" sdkver="0">
            <title>当前微信版本不支持展示该内容，请升级至最新版本。</title>
            <type>51</type>
            <url>https://support.weixin.qq.com/update/</url>
            <finderFeed>
                <nickname><![CDATA[Some creator]]></nickname>
                <desc><![CDATA[Video description #tag]]></desc>
            </finderFeed>
        </appmsg>
    </msg>
    """)
    manager = mock.Mock()
    manager._ = lambda s: s
    manager.get_node_text = SlaveMessageManager.get_node_text

    SlaveMessageManager.wechat_channels_msg(manager, mock.sentinel.msg, xml)

    manager.wechat_raw_link_msg.assert_called_once_with(
        mock.sentinel.msg, "[Channels] Some creator", "Video description #tag", "", "")
