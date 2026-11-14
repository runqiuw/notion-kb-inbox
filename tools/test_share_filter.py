"""user_note() must not throw away something the user actually wrote.

The filter used to hold bare platform names, so any thought that merely
mentioned 小红书 disappeared silently. Letting a template through is visible and
fixable; losing your own sentence is neither.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.pipeline import user_note, _SHARE_BOILERPLATE

KEEP = [
    "小红书上这个观点我不同意，作者混淆了相关性和因果",
    "B 站那个 up 主讲的 bilibili 弹幕文化其实挺有意思",
    "这篇要查看全文才知道结论，摘要写得有误导性",
    "world model 更适合承担高层 prediction",
    "这个网站强制打开APP，体验很差，但内容值得看",
]
DROP = [
    "6.66 复制打开抖音，看看【xxx】...再复制这段话，打开豆包App观看视频！",
    "这段文字复制后打开小红书即可查看",
    "长按复制此条消息，打开抖音搜索",
    "3 发布了一篇小红书笔记，复制打开小红书 App 查看",
]

for t in KEEP:
    assert user_note(t, "某文章", "正文" * 50) == t, f"误伤: {t}"
for t in DROP:
    assert user_note(t, "某文章", "正文" * 50) == "", f"没拦住: {t}"

# Still drops what it always dropped for non-keyword reasons.
assert user_note("", "t", "x") == ""
assert user_note("好", "t", "x") == ""            # too short to be a thought
assert user_note("正文正文", "t", "正文正文的开头") == ""   # echoes the article

# No bare platform name may creep back into the substring list -- that is what
# caused the false positives. They live in _PLATFORM_ONLY, matched whole.
for bad in ("小红书", "bilibili", "哔哩哔哩", "查看全文", "网页链接", "打开APP"):
    assert bad not in _SHARE_BOILERPLATE, f"裸平台名不该回到 substring 表: {bad}"

# A message that is nothing but a platform name is still the share sheet talking.
for t in ("- 哔哩哔哩", "网页链接", "【小红书】", "查看全文"):
    assert user_note(t, "某文章", "正文" * 50) == "", f"整条平台名没拦住: {t}"


# ---------- echoing the material back is not a thought ----------
# Observed in production: a 小红书 share pasted the title, a slab of the body,
# then its own tail. That is a substring of nothing, so the old exact-match
# test let it through and it was filed as a 随想.
TITLE = "ICLR 2027迎来Scaling Law!"
BODY = ("ICLR 2027 一年的投稿量或将超历史十几届的总和，AI科研已经迎来指数爆炸式增长，"
        "我们在2024年曾在《Scaling Laws in Scientific Discovery》尝试预测未来科研"
        "范式的转变，没想到还是太保守了，ICLR曲线这么陡！AI已经在AI本身、数学，生信等"
        "研究领域产生了颠覆性影响，甚至取得了重大突破，实验科学还能撑多久？")

leaked = f"{TITLE} {BODY[:80]} 跳转【小红书】看看这篇分享！"
assert user_note(leaked, TITLE, BODY) == "", "线上漏掉的这条必须被认出来"
assert user_note(TITLE, TITLE, BODY) == "", "纯复述标题"

# ...but a thought that quotes the article is still a thought. This is the
# case a blunter rule would destroy, and destroying it is the worse error.
quoting = f"「{BODY[:30]}」——这个推断跳步了，样本只有一年"
assert user_note(quoting, TITLE, BODY) == quoting, "引用原文的想法不能被吃掉"
own = "这条曲线的解释我不认同，投稿量涨是因为门槛降了不是因为 AI"
assert user_note(own, TITLE, BODY) == own
assert user_note("值得追一下这个数据来源", TITLE, BODY)

print("PASS")
