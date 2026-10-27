"""List or run one bundled example without inventing a second runtime path."""
from pathlib import Path
import runpy
import sys


EXAMPLES = {
    "google": ("google/google_search.py", "Google 搜索"),
    "boss": ("boss/boss.py", "BOSS 职位"),
    "fangdi": ("网上房地产-瑞数6/fangdi.py", "网上房地产（瑞数 6）"),
    "cebwm": ("光大理财-瑞数定制/cebwm.py", "光大理财（瑞数定制）"),
    "toutiao": ("今日头条/toutiao.py", "今日头条"),
    "douyin": ("抖音/douyin.py", "抖音精选视频流"),
    "xhs": ("xhs/xhs.py", "小红书 rednote 推荐流"),
    "geetest": ("极验/geetest_fullpage.py", "极验"),
    "yidun": ("网易易盾/yidun.py", "网易易盾"),
    "shumei": ("数美/shumei.py", "数美"),
    "tianyu": ("腾讯天御/tianyu.py", "腾讯天御"),
    "creepjs": ("creepjs/creepjs.py", "CreepJS"),
    "akamai": ("akamai/akamai.py", "Akamai Bot Manager 3.x"),
    "ali234": ("阿里234/taobao_bxua.py", "阿里 234 / 淘宝 bx-ua"),
}


def _root():
    # Installed layout: examples sit next to this file (ruyijs/examples/<name>).
    # Source tree: this file is python/ruyijs/examples/__main__.py and the
    # examples live at <repo>/examples. google ships in the package but is not
    # copied into the source-tree python/ruyijs/examples, so it tells them apart.
    local = Path(__file__).resolve().parent
    if (local / "google/google_search.py").is_file():
        return local
    return Path(__file__).resolve().parents[3] / "examples"


def _list():
    print("可用示例：")
    for name, (_, description) in EXAMPLES.items():
        print(f"  {name:<10} {description}")
    print("\n运行：python -m ruyijs.examples NAME [NAME 参数]")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "list"):
        _list()
        return 0
    name = argv.pop(0)
    item = EXAMPLES.get(name)
    if item is None:
        _list()
        print(f"\n未知示例：{name}", file=sys.stderr)
        return 2
    root = _root().resolve()
    script = (root / item[0]).resolve()
    if not script.is_file() or not script.is_relative_to(root):
        print(f"示例文件不存在：{script}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *argv]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
