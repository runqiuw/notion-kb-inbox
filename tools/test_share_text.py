"""Platform share blurbs must never be filed as the user's own thought.

A 小红书 share pastes the post's own title and a "打开【小红书】浏览笔记" line
next to the link. Filing that as 随想 puts words in the user's mouth, which
plan section 29.3 forbids: only text the user confirmed represents the user.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import pipeline

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("分享模板 → 不是想法")
XHS = "AI infra劝退贴 今天面了某大厂的日常实习，对infra有...  \n这段文字复制好，打开【小红书】浏览笔记。"
check("小红书分享文案被挡住", pipeline.user_note(XHS) == "", pipeline.user_note(XHS)[:40])
for blurb in ["复制打开抖音，看看【某某】的作品", "- 哔哩哔哩", "长按复制此条消息",
              "发布了一篇小红书笔记，快来看吧！", "网页链接"]:
    check(f"{blurb[:12]!r} 被挡住", pipeline.user_note(blurb) == "")

print("重复材料自身内容 → 不是想法")
check("与标题重复被挡住",
      pipeline.user_note("AI infra劝退贴", "AI infra劝退贴", "") == "")
check("正文里已有的句子被挡住",
      pipeline.user_note("今天面了某大厂的日常实习", "标题", "今天面了某大厂的日常实习，对infra有些想法") == "")

print("真正的想法 → 保留")
desktop = '38 【Dario在17年的认知今天依然超前 - 大模法师 | 小红书 - 你的生活兴趣社区】 😆 R8qUAsbbLUbcUJw 😆'
check('桌面分享标题与口令不是想法', pipeline.user_note(desktop) == '')
check('标题含第一人称仍是桌面模板', pipeline.user_note(desktop.replace('Dario在17年的认知今天依然超前','我为什么不做3D了')) == '')
check('桌面模板后内联真实评论保留',pipeline.user_note(desktop+' 我不认同这个判断')=='我不认同这个判断')
check('桌面模板后换行真实评论保留',pipeline.user_note(desktop+'\n支持这个解释，但证据太少')=='支持这个解释，但证据太少')
for real in ["我觉得这个结论反了", "这篇的 assumption 有点强，值得再看看",
             "和我之前想的 world model 那条冲突"]:
    check(f"{real[:12]!r} 保留", pipeline.user_note(real, "某标题", "某正文") == real)

print("边界")
check("空字符串", pipeline.user_note("") == "")
check("只有空白", pipeline.user_note("   \n ") == "")
check("过短的残渣被挡住", pipeline.user_note("好", "标题", "正文") == "")

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
