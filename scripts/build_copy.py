#!/usr/bin/env python3
"""
build_copy.py — 参考リールURLから「完コピ用カード」を発行するCLI（リール間コピー）

アフレコ版(build_shorts.py)が「アフレコ音声の喋りのタイミング」で秒数を決めるのに対し、
こちらは「参考リールのカット割り（シーンの切り替わり）」で秒数を決める。
参考リールと全く同じ秒数のカードが並ぶので、各カードに自分の素材をはめるだけで
リズム・テンポが完コピできる。素材・音声・テロップは自分のものを入れる（尺だけ再現）。

使い方:
  python scripts/build_copy.py <参考リールURL> --project <name> [--materials <素材フォルダ>]
  python scripts/build_copy.py <ローカル動画パス> --project <name>   # URLの代わりに動画ファイルでも可

処理:
  1. yt-dlp で参考リールを取得 → work/<project>/reference.mp4
     （URLでなくローカル動画パスを渡した場合はコピーする）
  2. ffmpeg のシーン検出でカットの切り替わり点を抽出
  3. 各カット＝1カードとして、参考と同じ秒数で timeline.json を発行（全カード未割当て）
  4. materials.json を用意（素材はエディタの「🔄 動画素材フォルダを更新」で取り込む）

このあと editor_server.py <project> でエディタを開き、各カードに素材をはめる。
カードごとの秒数はエディタの数字入力で変更できる。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windowsのcp932コンソールで絵文字printが落ちるのを防ぐ
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WORK_ROOT = Path.home() / "reel-henshu-afreco" / "work"

# シーン検出のしきい値（ffmpeg scdet のスコア0〜100。小さいほど敏感＝カットを細かく拾う）。
# エディタの自動分割と同じ scdet 方式に統一。実測の分布で、はっきりしたハードカットに加え
# 「絵の変化が控えめだが確かに切れている中程度カット」（スコア約5、実素材で確認）まで拾い、
# かつ同構図ジャンプカット/手の動き等のノイズ（スコア1〜3）は拾わない境目が 5 前後。
# それより弱い同構図ジャンプカットは原理的に拾えないので、エディタの「自動分割」で分ける。
DEFAULT_SCENE_THRESHOLD = 5.0
# これより短いカットは手前のカットに吸収する（一瞬のフラッシュ・誤検出でカードが
# 大量に増えるのを防ぐ）。--min-dur で変更可。
DEFAULT_MIN_DURATION = 0.4


def fmt_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec % 60
    return f"{m}分{s:.1f}秒" if m else f"{s:.1f}秒"


def _run_ytdlp(url: str, out_path: Path, use_cookies: bool) -> tuple:
    """参考リールを out_path に取得する。yt-dlp は venv に入れたものを
    `python -m yt_dlp` で呼ぶ（OS非依存・PATHにyt-dlp不要。bunseki-relと同方式）。
    まずcookieなし → 失敗したらchromeのcookieで再試行、は呼び出し側で行う。"""
    cmd = [sys.executable, "-m", "yt_dlp", "-o", str(out_path), "--no-playlist",
           "-f", "mp4/bestvideo+bestaudio/best", "--no-progress", url]
    if use_cookies:
        cmd[3:3] = ["--cookies-from-browser", "chrome"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return r.returncode == 0, (r.stderr or "").strip()[-500:]
    except subprocess.TimeoutExpired:
        return False, "timeout (300s)"


def fetch_reference(url_or_path: str, work_dir: Path) -> Path:
    """参考リールを work_dir/reference.mp4 に用意する。
    ローカルの動画ファイルパスが渡された場合はコピー、それ以外はyt-dlpで取得。"""
    ref_path = work_dir / "reference.mp4"
    local = Path(url_or_path).expanduser()
    if local.exists() and local.is_file():
        print(f"  ローカル動画を参考として使用: {local}")
        if local.resolve() != ref_path.resolve():
            shutil.copy(local, ref_path)
        return ref_path

    print(f"  参考リールを取得中: {url_or_path}")
    ok, err = _run_ytdlp(url_or_path, ref_path, use_cookies=False)
    if not ok and not ref_path.exists():
        print("  cookieなしで失敗 → Chromeのcookieで再試行")
        ok, err = _run_ytdlp(url_or_path, ref_path, use_cookies=True)
    # -o のテンプレなしで拡張子が変わる場合に備えて回収
    if not ref_path.exists():
        cand = sorted(work_dir.glob("reference.*"))
        if cand:
            cand[0].rename(ref_path)
    if not ref_path.exists():
        print(f"❌ 参考リールの取得に失敗しました: {err.splitlines()[-1] if err else 'unknown'}")
        sys.exit(1)
    return ref_path


def get_duration(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(video)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def detect_scene_cuts(video: Path, threshold: float) -> list:
    """ffmpeg scdet で「カットの切り替わり時刻（秒）」の一覧を返す。
    scdet はフレームごとのシーン変化スコア(0〜100)を出す。エディタの自動分割
    (_scene_scores/detect_auto_splits)と同じ方式で、旧 select='gt(scene,..)' が
    取りこぼしていた明確なカット（実測 スコア約10のカット等）も拾える。
    近接カット（強いカットのトレーリングフレーム等）は先頭の1つにまとめる。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(video),
         "-vf", "scdet=threshold=0,metadata=print", "-an", "-f", "null", "-"],
        capture_output=True, text=True)
    scores, last = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"lavfi\.scd\.score=([\d.]+)", line)
        if m:
            last = float(m.group(1)); continue
        m = re.search(r"lavfi\.scd\.time=([\d.]+)", line)
        if m and last is not None:
            scores.append((float(m.group(1)), last)); last = None
    cuts = sorted(t for t, s in scores if s >= threshold)
    merged = []
    for t in cuts:
        if merged and t - merged[-1] < 0.2:  # 0.2秒以内の近接カットは1つにまとめる
            continue
        merged.append(round(t, 3))
    return merged


def build_segments(total: float, cuts: list, min_dur: float) -> list:
    """カット時刻の一覧から [(start, duration), ...] の区間を作る。
    短すぎる区間は手前に吸収してカードが増えすぎないようにする。"""
    bounds = [0.0] + [c for c in cuts if 0.0 < c < total] + [total]
    bounds = sorted(set(round(b, 3) for b in bounds))
    segs = []
    for a, b in zip(bounds, bounds[1:]):
        dur = round(b - a, 3)
        if dur < min_dur and segs:
            # 短い区間は手前のカードに足す（切り替わりが速すぎる誤検出対策）
            ps, pd = segs[-1]
            segs[-1] = (ps, round(pd + dur, 3))
        else:
            segs.append((round(a, 3), dur))
    # 先頭が短すぎる場合の保険
    if segs and segs[0][1] < min_dur and len(segs) > 1:
        s0, d0 = segs.pop(0)
        s1, d1 = segs[0]
        segs[0] = (s0, round(d0 + d1, 3))
    return segs


_TELOP_HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ありがとうございました",
    "チャンネル登録よろしくお願いします", "最後までご覧いただきありがとうございました",
}


def add_ref_telop(cards: list, ref_path: Path) -> int:
    """参考リールのナレーションを文字起こしし、各カードの時間帯に表示されている
    テロップ（＝発話フレーズ単位）の全文を、そのカードの『テロップ見本』(ref_text)に入れる。

    重要: 文字を1文字ずつカードの秒数で切ると「もう作っ」「た?お」のように語の途中で
    切れてしまう。実際のリールは1つのテロップが複数カット（＝複数カード）にまたがって
    表示されるので、映像ベースで割ったカードでは、同じフレーズに重なる複数カードには
    「同じフレーズ全文」が入るのが正しい。そこでフレーズ（Whisperのsegment）と各カードの
    時間の重なりを見て、一番重なるフレーズの全文をそのカードに割り当てる。

    エディタでは薄いグレーのプレースホルダとして表示し、ユーザーが自分の文言に入れ替える前提。
    音声が無い/文字起こし失敗時は ref_text を空にする（後で手入力できる）。"""
    for c in cards:
        c["ref_text"] = ""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        sys.path.insert(0, str(Path(__file__).parent / "_vendor"))
        from platform_utils import transcribe_ja
        print("  参考リールのテロップ（ナレーション）を読み取り中…（初回はモデルDLで時間がかかる）")
        result = transcribe_ja(str(ref_path))
        phrases = [(float(s["start"]), float(s["end"]), (s.get("text") or "").strip())
                   for s in result.get("segments", []) if (s.get("text") or "").strip()]
        if not phrases:
            print("  （音声・ナレーションが検出できなかったのでテロップ見本は空にしました）")
            return 0
        n_filled = 0
        for c in cards:
            # このカードの時間帯に一番長く重なっているフレーズの全文を採用する
            best_txt, best_ov = "", 0.0
            for ps, pe, txt in phrases:
                ov = min(c["end"], pe) - max(c["start"], ps)
                if ov > best_ov:
                    best_ov, best_txt = ov, txt
            if best_ov > 0 and best_txt and best_txt not in _TELOP_HALLUCINATIONS:
                c["ref_text"] = best_txt
                n_filled += 1
        return n_filled
    except Exception as e:
        print(f"  ⚠️ テロップ読み取りをスキップしました（後で手入力できます）: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="参考リールURLから完コピ用カードを発行する")
    parser.add_argument("reference", help="参考リールのURL（またはローカル動画ファイルのパス）")
    parser.add_argument("--project", required=True, help="プロジェクト名（work/<name>/に出力）")
    parser.add_argument("--materials", default="",
                        help="自分の素材フォルダのパス（後からエディタで指定/取り込みも可）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCENE_THRESHOLD,
                        help=f"シーン検出の感度（scdetスコア0〜100・既定{DEFAULT_SCENE_THRESHOLD}。"
                             f"小さいほどカットを細かく拾う。同構図ジャンプカットは"
                             f"下げても拾いにくいのでエディタの自動分割で分ける）")
    parser.add_argument("--min-dur", type=float, default=DEFAULT_MIN_DURATION,
                        help=f"この秒数より短いカットは手前に吸収（既定{DEFAULT_MIN_DURATION}）")
    args = parser.parse_args()

    work_dir = WORK_ROOT / args.project
    work_dir.mkdir(parents=True, exist_ok=True)

    print("===================================================")
    print("  リール間コピー（完コピ用カード発行）")
    print("===================================================")
    print(f"  参考      : {args.reference}")
    print(f"  プロジェクト: {args.project}")
    print("===================================================\n")

    print("【STEP 1/3】 参考リールを用意中...")
    ref_path = fetch_reference(args.reference, work_dir)
    total = get_duration(ref_path)
    print(f"  参考リール尺: {fmt_time(total)}\n")

    print("【STEP 2/3】 カット割り（シーン検出）を解析中...")
    cuts = detect_scene_cuts(ref_path, args.threshold)
    segs = build_segments(total, cuts, args.min_dur)
    if not segs:
        segs = [(0.0, round(total, 3))]
    print(f"  {len(segs)}カットを検出（感度{args.threshold} / 最短{args.min_dur}秒）\n")

    print("【STEP 3/3】 完コピ用カードを発行中...")
    cards = []
    t = 0.0
    for i, (seg_start, dur) in enumerate(segs, start=1):
        cards.append({
            "id": i,
            "text": "",
            "char_count": 0,
            "start": round(t, 3),
            "end": round(t + dur, 3),
            "clips": [],            # 未割当て（素材はエディタではめる）
            "tag_filter": "either",
            "ref_in": round(seg_start, 3),   # 参考リール上でのこのカットの開始（お手本プレビュー用）
            "ref_dur": round(dur, 3),        # 参考リール上でのこのカットの尺
        })
        print(f"  #{i} [{cards[-1]['start']:.2f}-{cards[-1]['end']:.2f}s] {dur:.2f}秒")
        t += dur

    # 参考リールのテロップ（ナレーション）を各カードの見本として入れる
    n_telop = add_ref_telop(cards, ref_path)
    if n_telop:
        print(f"  {n_telop}/{len(cards)}カードにテロップ見本を入れました（エディタで薄いグレー表示・修正前提）")

    (work_dir / "timeline.json").write_text(
        json.dumps({
            "cards": cards,
            "voiceover_path": None,
            "copy_mode": True,
            "reference_video": "reference.mp4",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # materials.json を用意（materials_dir を必ず記録する）
    materials_json = work_dir / "materials.json"
    mat_dir = Path(args.materials).expanduser() if args.materials else None
    existing_mat = json.loads(materials_json.read_text(encoding="utf-8")) if materials_json.exists() else {}
    materials_json.write_text(
        json.dumps({"materials_dir": str(mat_dir) if mat_dir else existing_mat.get("materials_dir", ""),
                    "clips": existing_mat.get("clips", [])}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 素材フォルダが指定され、存在すれば、この場で取り込んでおく。
    # エディタを開いた時点で各カードのプルダウンに素材が並ぶので、初めて使う人が
    # 「🔄 動画素材フォルダを更新」ボタンを探さなくてよい（＝すぐ素材をはめられる）。
    imported = 0
    if mat_dir and mat_dir.exists():
        print("\n  指定された素材フォルダを取り込み中...")
        try:
            import editor_server as es  # 同フォルダのエンジンを流用
            es.work_dir = work_dir
            es.materials_dir = mat_dir
            res = es.scan_and_register_materials()
            imported = len(res.get("clips", []))
            print(f"  素材を{imported}件取り込みました（{mat_dir}）")
        except Exception as e:
            print(f"  ⚠️ 素材の自動取り込みに失敗しました（エディタの🔄で取り込めます）: {e}")

    print(f"\n===================================================")
    print(f"  ✅ カード発行完了！")
    print(f"===================================================")
    print(f"  {len(cards)}カード / 合計 {fmt_time(t)}（参考リールと同じ尺）")
    if imported:
        print(f"  素材{imported}件を取り込み済み → エディタで各カードにはめてください")
    else:
        print(f"  次: エディタで「🔄 動画素材フォルダを更新」→ 各カードに素材をはめる")


if __name__ == "__main__":
    main()
