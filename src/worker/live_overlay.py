"""直播内遮挡面板的只读识别；不靠颜色或任意“礼物”字样猜测点击位置。"""
from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

PACKAGE = 'com.ss.android.ugc.aweme'


@dataclass(frozen=True)
class OverlayEvidence:
    kind: str
    markers: tuple[str, ...]


def classify_live_overlay(xml: str) -> OverlayEvidence | None:
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    nodes = [n for n in root.iter('node') if n.get('package') == PACKAGE]
    texts = {n.get(key, '').strip() for n in nodes for key in ('text','content-desc')}
    texts.discard('')
    # 面板标题 + 面板内部操作/统计至少两个独立标记；聊天和顶部小入口不够。
    titles = [s for s in texts if re.search(r'甄选展馆|礼物展馆',s) and len(s)<80]
    markers = [s for s in texts if any(m in s for m in ('展馆贡献','快速点亮','款可满贯','主播心愿')) and len(s)<80]
    if titles and markers and len(set(titles+markers))>=2:
        return OverlayEvidence('gift_gallery',tuple(sorted(set(titles+markers))))
    # 实机粉丝团等级页：WebView 标题 + 团力值 + 原生关闭遮罩共同确认。
    # 顶部轮播入口或弹幕中出现“粉丝团”不能触发返回键。
    fan_title = any(n.get('class') == 'android.webkit.WebView'
                    and n.get('text', '').strip() == '粉丝团等级' for n in nodes)
    fan_stats = any('已累计' in s and '团力值' in s for s in texts)
    native_close = any(n.get('resource-id', '').rsplit('/', 1)[-1] == 'a2k'
                       and n.get('content-desc') == '关闭' and n.get('clickable') == 'true'
                       for n in nodes)
    if fan_title and fan_stats and native_close:
        return OverlayEvidence('fan_club_panel', ('fans_level_webview', 'group_power', 'native_close_mask'))
    # 主播资料卡，与普通直播顶部头像区区分。
    has_card_avatar = any(n.get('content-desc','').endswith('的头像')
                          and n.get('resource-id','').rsplit('/',1)[-1] in {'spf','avatar','profile_avatar'} for n in nodes)
    has_card_stats = any('关注' in s and '粉丝' in s for s in texts)
    if has_card_avatar and has_card_stats:
        return OverlayEvidence('anchor_profile_card',('profile_avatar','following_and_followers'))
    # 当前客户端的展馆由自绘视图渲染，UI树可能完全没有标题文字。
    # 只接受同一原生面板容器内的全屏关闭遮罩和大面积底部内容，不猜颜色。
    for container in nodes:
        if container.get('resource-id', '').rsplit('/', 1)[-1] != 'a2j':
            continue
        children = [n for n in container.iter('node') if n.get('package') == PACKAGE]
        masks = [n for n in children if n.get('resource-id', '').rsplit('/', 1)[-1] == 'a2k'
                 and n.get('content-desc') == '关闭' and n.get('clickable') == 'true']
        panels = [n for n in children if n.get('resource-id', '').rsplit('/', 1)[-1] == 'a2q']
        if len(masks) != 1 or len(panels) != 1:
            continue
        def bounds(node):
            match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
            return tuple(map(int, match.groups())) if match else None
        mask, panel = bounds(masks[0]), bounds(panels[0])
        if not mask or not panel:
            continue
        x0, y0, x1, y1 = mask
        left, top, right, bottom = panel
        width, height = x1-x0, y1-y0
        if (width > 0 and height > 0 and 1.4 < height/width < 2.5
                and x0 <= left < right <= x1 and y0 <= top < bottom <= y1
                and right-left >= .9*width and .08*height <= top-y0 <= .6*height
                and bottom >= y0+.95*height and bottom-top >= .4*height
                and any(n.get('class') in {'android.widget.ScrollView', 'android.webkit.WebView'}
                        for n in panels[0].iter('node'))):
            return OverlayEvidence('live_bottom_sheet', ('native_live_container', 'full_close_mask', 'large_bottom_content'))
    return None
