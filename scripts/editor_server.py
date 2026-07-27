#!/usr/bin/env python3
"""
editor_server.py — ショート動画カード型タイムラインエディタのローカルサーバー
使い方: python scripts/editor_server.py <project>
  work/<project>/timeline.json と materials.json を読み込んで配信する。
"""
import concurrent.futures
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if hasattr(sys.stdout, "reconfigure"):  # Windowsのcp932コンソールで絵文字printが落ちるのを防ぐ
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if os.path.isdir("/opt/homebrew/bin"):  # Macのみ（Windows/Linuxは既存PATHのffmpegを使う）
    os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from platform_utils import video_encoder, heic_to_jpg  # noqa: E402  OS別

PORT = 8766
SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT = SCRIPTS_DIR.parent
EDITOR_DIR = REPO_ROOT / "editor"
WORK_ROOT = REPO_ROOT / "work"

# config.json で上書き可能（インストール後に各自設定）
_config_path = REPO_ROOT / "config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
_sound = _config.get("sound_dir") or (Path.home() / "reel-henshu-afreco" / "sounds")
SOUND_DIR = Path(_sound).expanduser()  # 空/未設定なら各自の ~/reel-henshu-afreco/sounds

sys.path.insert(0, str(SCRIPTS_DIR))
from assign_clips import (  # noqa: E402
    _score, _signature, detect_keywords, pick_in_point, _pick_in_point_for_use, get_clips,
)
from render import render_timeline  # noqa: E402

project: str = ""
work_dir: Path = None
materials_dir: Path = None


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_VIDEO_EXTS = (".mp4", ".mov")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")   # ffmpegが直接扱える画像
_HEIC_EXTS = (".heic", ".heif")                     # sipsでjpgに変換して扱う


def _convert_heic_to_jpg(src: Path) -> Path:
    """HEIC/HEIFはffmpegが直接扱えないので、同フォルダにjpgを作って返す。
    Mac=sips、Windows等=pillow-heif（platform_utils.heic_to_jpgが吸収）。"""
    dst = src.with_suffix(".jpg")
    if not dst.exists():
        try:
            heic_to_jpg(src, dst)
        except Exception as e:
            print(f"  ⚠️ HEIC変換に失敗: {src.name}: {e}")
            return None
    return dst if dst.exists() else None


def _make_image_thumb(src: Path, base: str, frames_dir: Path) -> list:
    out = frames_dir / f"{base}_0.jpg"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(src),
                    "-vf", "scale=-2:640", "-frames:v", "1", "-q:v", "4", str(out)],
                   capture_output=True)
    if out.exists() and out.stat().st_size > 0:
        return [{"path": str(out), "t": 0}]
    return []


def _probe_duration(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_frames(src: Path, base: str, dur: float, frames_dir: Path, n: int = 8) -> list:
    frames = []
    if dur <= 0:
        return frames
    for i in range(n):
        t = round(dur * (i + 0.5) / n, 2)
        out = frames_dir / f"{base}_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", str(t), "-i", str(src),
             "-frames:v", "1", "-vf", "scale=-2:640", "-q:v", "4", str(out)],
            capture_output=True)
        if out.exists() and out.stat().st_size > 0:
            frames.append({"path": str(out), "t": t})
    return frames


def scan_and_register_materials() -> dict:
    """materials_dir と materials.json を同期する。フォルダにある未登録の動画/画像を
    追加（duration取得＋サムネ抽出）し、フォルダから消えた素材の登録は削除する。
    タグ付けはしない（tag=either / memo=None）。手動の「動画素材フォルダを更新」ボタンから呼ぶ。"""
    mjson_path = work_dir / "materials.json"
    data = _load_json(mjson_path)
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    if not materials_dir.exists():
        data["_added"] = 0
        data["_removed"] = 0
        return data

    # フォルダから消えた素材の登録を削除する（幽霊素材の掃除）
    removed_files = [c["file"] for c in data["clips"]
                     if not (materials_dir / c["file"]).exists()]
    if removed_files:
        data["clips"] = [c for c in data["clips"]
                         if (materials_dir / c["file"]).exists()]
        # 消えた素材を使っているカードの割り当ても外す（未割当てに戻す）
        tl_path = work_dir / "timeline.json"
        if tl_path.exists():
            tl = _load_json(tl_path)
            gone = set(removed_files)
            changed = False
            for card in tl.get("cards", []):
                kept = [cl for cl in card.get("clips", []) if cl["file"] not in gone]
                if len(kept) != len(card.get("clips", [])):
                    card["clips"] = kept
                    changed = True
            if changed:
                _save_json(tl_path, tl)

    existing = {c["file"] for c in data["clips"]}

    all_exts = _VIDEO_EXTS + _IMAGE_EXTS + _HEIC_EXTS
    files = sorted([f for f in materials_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in all_exts
                    and not f.name.startswith(".")])
    next_seq = max([c.get("sequence_no", 0) for c in data["clips"]], default=0) + 1

    # まず新規ファイルを確定する（HEIC変換は直列＝軽い。重い処理は後段で並列化）
    new_files = []
    for f in files:
        ext = f.suffix.lower()
        # HEICはjpgに変換して、以後jpgとして扱う
        if ext in _HEIC_EXTS:
            jpg = _convert_heic_to_jpg(f)
            if jpg is None:
                continue
            f, ext = jpg, ".jpg"
        if f.name in existing:
            continue
        existing.add(f.name)
        new_files.append((f, ext))

    def _process(item):
        # duration取得＋フレーム抽出（ffmpeg呼び出し＝重い）を1ファイル分。
        # ファイルごとに独立なのでスレッドプールで並列化できる。
        f, ext = item
        if ext in _VIDEO_EXTS:
            dur = _probe_duration(f)
            frames = _extract_frames(f, f.name, dur, frames_dir)
            return {"file": f.name, "duration": dur, "frames": frames,
                    "motion_ts": [], "tag": "either", "memo": None, "good_in": 0.5}
        frames = _make_image_thumb(f, f.name, frames_dir)
        return {"file": f.name, "duration": 0, "frames": frames,
                "motion_ts": [], "tag": "either", "memo": None,
                "good_in": 0.0, "is_image": True}

    import concurrent.futures
    if new_files:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(_process, new_files))
        for clip in results:  # 元のファイル名順を保ったまま連番を振る
            clip["sequence_no"] = next_seq
            data["clips"].append(clip)
            next_seq += 1

    added = len(new_files)
    if added or removed_files:
        _save_json(mjson_path, data)

    # 追加された動画のプレビュープロキシを裏で先に作っておく（初回▶再生の待ちを無くす）
    new_videos = [f.name for f, ext in new_files if ext in _VIDEO_EXTS]
    if new_videos:
        def _prewarm_new():
            for name in new_videos:
                try:
                    _get_preview_proxy(name)
                except Exception:
                    pass
        threading.Thread(target=_prewarm_new, daemon=True).start()

    data["_added"] = added
    data["_removed"] = len(removed_files)
    return data


def _get_preview_proxy(filename: str) -> Path:
    """カメラ直撮り素材は10bit HEVC等、ブラウザが直接デコードできない形式の
    ことがある（実機テストで発覚：MEDIA_ELEMENT_ERROR）。エディタのプレビュー用に
    H.264の軽量プロキシを1回だけ生成してキャッシュする（高さ960に縮小、最終書き出し
    には影響しない。final.mp4は常にrender.pyが元素材から作る）"""
    proxies_dir = work_dir / "proxies"
    proxies_dir.mkdir(parents=True, exist_ok=True)
    proxy_path = proxies_dir / f"{filename}.proxy.mp4"

    if not proxy_path.exists():
        src = materials_dir / filename
        # 生成中にサーバーが止まる/エディタを閉じると、直書きでは壊れた半端プロキシが
        # proxy_pathに残り、次回それを"完成品"として再利用してしまい（moov atom not found）
        # その素材が黒画面/エラーになる。一時ファイルに書き成功時のみリネームする。
        tmp_path = proxies_dir / f"{filename}.partial.mp4"
        if src.suffix.lower() in _IMAGE_EXTS:
            # 静止画は3秒のループ動画プロキシにする（エディタの<video>で再生できるように）
            cmd = ["ffmpeg", "-y",
                   "-loop", "1", "-framerate", "30", "-t", "3", "-i", str(src),
                   "-f", "lavfi", "-t", "3", "-i", "anullsrc=r=48000:cl=stereo",
                   "-map", "0:v", "-map", "1:a",
                   "-vf", "scale=-2:960",
                   *video_encoder(),
                   "-c:a", "aac", "-b:a", "128k",
                   str(tmp_path)]
        else:
            cmd = ["ffmpeg", "-y", "-i", str(src),
                   "-vf", "scale=-2:960",
                   *video_encoder(),
                   "-c:a", "aac", "-b:a", "128k",
                   str(tmp_path)]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            os.replace(tmp_path, proxy_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return proxy_path


def _scene_scores(ref_path: Path) -> list:
    """参考リール全体の各フレームのシーン変化スコア [(time, score), ...] を返す
    （ffmpeg scdet。scoreは0〜100、ハードカットで大きく跳ねる）。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(ref_path),
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
    return scores


def detect_splits(ref_path: Path, start: float, dur: float, n: int) -> list:
    """参考リールの [start, start+dur] 区間を、実際のカット境界で n 個に分ける。
    区間内のシーン変化スコアが強い順に n-1 個の境界を選び（均等割りにしない＝
    元リールの不均等なカット尺を再現）、足りない分だけ一番広い隙間を等分して補う。
    戻り値: [(ref_in, ref_dur), ...]（n個・合計=dur）。"""
    end = start + dur
    edge = min(0.12, dur * 0.12)      # 区間の端すぎる検出は境界にしない
    min_gap = min(0.20, dur / (n * 2))  # 境界どうしが近すぎないように
    cand = [(t, s) for (t, s) in _scene_scores(ref_path) if start + edge < t < end - edge]
    cand.sort(key=lambda x: -x[1])  # スコア降順
    picked = []
    for t, _ in cand:
        if len(picked) >= n - 1:
            break
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(round(t, 3))
    picked.sort()
    bounds = [round(start, 3)] + picked + [round(end, 3)]
    # 検出された境界が n-1 に満たない場合は、一番広い区間を半分に割って数を合わせる
    while len(bounds) - 1 < n:
        gi = max(range(len(bounds) - 1), key=lambda i: bounds[i + 1] - bounds[i])
        mid = round((bounds[gi] + bounds[gi + 1]) / 2, 3)
        bounds.insert(gi + 1, mid)
    segs = [(round(bounds[i], 3), round(bounds[i + 1] - bounds[i], 3)) for i in range(n)]
    return segs


AUTO_SPLIT_SCORE = 1.0    # この強さ以上のシーン変化を「カットの切れ目」の候補とみなす（自動分割用）
AUTO_SPLIT_MIN_SEG = 0.25  # 自動分割後の各カットの最短秒数。これ未満になる切れ目は採らない


def detect_auto_splits(ref_path: Path, start: float, dur: float, max_cuts: int = 7) -> list:
    """参考リールの [start, start+dur] 区間の中に実際に何カットあるかを自動判定し、
    その数だけの ref区間 [(ref_in, ref_dur), ...] を返す（ユーザーが枚数を数えなくてよい）。

    誤検出対策: scdetのスコアだけでは「本物のカット」と「1ショット内の速い動き」を
    見分けられず、短いカードを刻みすぎる（実測: 0.53秒のカードが0.16秒刻みで3分割された）。
    そこで自動分割は保守的にし、各カットが AUTO_SPLIT_MIN_SEG 秒以上になる切れ目だけ採用する
    （区間の端からも・互いにも MIN_SEG 秒以上離す）。過剰分割よりは分割しない側に倒す
    （足りなければ手動分割で足せる）。追加カットが無ければ1区間（=分割不要）を返す。"""
    end = start + dur
    mg = AUTO_SPLIT_MIN_SEG
    if dur < 2 * mg:  # 区間が短すぎて2つに割れない
        return [(round(start, 3), round(dur, 3))]
    cand = [(t, s) for (t, s) in _scene_scores(ref_path)
            if start + mg <= t <= end - mg and s >= AUTO_SPLIT_SCORE]
    cand.sort(key=lambda x: -x[1])  # 強い順
    picked = []
    for t, _ in cand:
        if len(picked) >= max_cuts:
            break
        if all(abs(t - p) >= mg for p in picked):  # 既存の切れ目ともMIN_SEG以上離す
            picked.append(round(t, 3))
    picked.sort()
    bounds = [round(start, 3)] + picked + [round(end, 3)]
    return [(round(bounds[i], 3), round(bounds[i + 1] - bounds[i], 3))
            for i in range(len(bounds) - 1)]


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    # ── GET ────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(EDITOR_DIR / "index.html", "text/html; charset=utf-8")

        elif path == "/api/timeline":
            tl = _load_json(work_dir / "timeline.json")
            tl["final_exists"] = (work_dir / "final.mp4").exists()
            vo = tl.get("voiceover_path")
            if vo:
                try:
                    import subprocess as _sp, json as _json
                    r = _sp.run(["ffprobe","-v","error","-show_entries","format=duration",
                                 "-of","json", vo], capture_output=True, text=True, check=True)
                    tl["voiceover_duration"] = float(_json.loads(r.stdout)["format"]["duration"])
                except Exception:
                    pass
            self._send_json(tl)

        elif path == "/api/materials":
            self._send_json(_load_json(work_dir / "materials.json"))

        elif path == "/api/clip":
            filename = query.get("file", [None])[0]
            if not filename:
                self.send_response(400)
                self.end_headers()
                return
            try:
                proxy_path = _get_preview_proxy(filename)
            except subprocess.CalledProcessError:
                self.send_response(500)
                self.end_headers()
                return
            self._serve_range_file(proxy_path)

        elif path == "/api/frame":
            clip_file = query.get("clip", [None])[0]
            n = int(query.get("n", ["0"])[0])
            materials = _load_json(work_dir / "materials.json")
            clip = next((c for c in materials["clips"] if c["file"] == clip_file), None)
            if not clip or not clip.get("frames"):
                self.send_response(404)
                self.end_headers()
                return
            frames = clip["frames"]
            frame = frames[min(n, len(frames) - 1)]
            # framesは[{"path","t"}]。pathは~/reel-henshu-afrecoからの相対。
            # 古いmaterials.jsonには絶対パスが入っていることがあり、リポジトリの
            # フォルダを移動・改名すると指す先が消える。その場合は同名ファイルを
            # プロジェクトのframes/内から探して自動復旧する（voiceoverと同じ方針）。
            frame_path = (SCRIPTS_DIR.parent / frame["path"]).resolve()
            if not frame_path.exists():
                frame_path = work_dir / "frames" / Path(frame["path"]).name
            if not frame_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            self._send_file(frame_path, "image/jpeg")

        elif path == "/api/final":
            final_path = work_dir / "final.mp4"
            if not final_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            self._serve_range_file(final_path)

        elif path == "/api/detect_splits":
            # リール間コピーのカード分割用。参考リールの [start, start+dur] を、
            # 実際のカット境界で n 個に分けた ref区間の一覧を返す（均等割りにしない）。
            ref_path = work_dir / "reference.mp4"
            n_raw = query.get("n", ["2"])[0]
            auto = (n_raw == "auto")
            try:
                start = float(query.get("start", ["0"])[0])
                dur = float(query.get("dur", ["0"])[0])
                n = 0 if auto else int(n_raw)
            except ValueError:
                self.send_response(400); self.end_headers(); return
            if not ref_path.exists() or dur <= 0 or (not auto and n < 2):
                self._send_json({"ok": False, "segments": []})
                return
            try:
                segs = detect_auto_splits(ref_path, start, dur) if auto \
                    else detect_splits(ref_path, start, dur, n)
                self._send_json({"ok": True,
                                 "segments": [{"ref_in": s, "ref_dur": d} for s, d in segs]})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e), "segments": []})

        elif path == "/api/refvideo":
            # リール間コピーの「お手本プレビュー」用。参考リール(reference.mp4)を
            # Range対応で配信する（全体版。カード単位は /api/refclip）。
            ref_path = work_dir / "reference.mp4"
            if not ref_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            self._serve_range_file(ref_path)

        elif path == "/api/refclip":
            # お手本プレビュー（カード単位）。参考リールから [in, in+dur] だけを切り出した
            # 動画を配信する。フロントは <video loop> でネイティブにループ再生するので、
            # 構造上そのカードのカット以外は絶対に映らない（次シーンにはみ出さない）。
            ref_path = work_dir / "reference.mp4"
            try:
                rin = float(query.get("in", ["0"])[0])
                rdur = float(query.get("dur", ["0"])[0])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            if not ref_path.exists() or rdur <= 0:
                self.send_response(404); self.end_headers(); return
            clips_dir = work_dir / "refclips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            out = clips_dir / f"{rin:.3f}_{rdur:.3f}.mp4"
            if not out.exists():
                tmp = clips_dir / f".{rin:.3f}_{rdur:.3f}.part.mp4"
                cmd = ["ffmpeg", "-nostdin", "-y", "-ss", f"{rin:.3f}", "-i", str(ref_path),
                       "-t", f"{rdur:.3f}", "-vf", "scale=-2:960", *video_encoder(),
                       "-c:a", "aac", "-b:a", "128k", str(tmp)]
                try:
                    subprocess.run(cmd, capture_output=True, check=True)
                    os.replace(tmp, out)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    self.send_response(500); self.end_headers(); return
            self._serve_range_file(out)

        elif path == "/api/sounds":
            sounds = []
            if SOUND_DIR.exists():
                for cat_dir in sorted(SOUND_DIR.iterdir()):
                    if cat_dir.is_dir() and not cat_dir.name.startswith("."):
                        for f in sorted(cat_dir.iterdir()):
                            if f.suffix.lower() == ".mp3":
                                sounds.append({
                                    "name": f.stem,
                                    "path": f"{cat_dir.name}/{f.name}",
                                    "category": cat_dir.name,
                                })
            self._send_json(sounds)

        elif path == "/api/sound":
            rel = query.get("file", [""])[0]
            sound_path = SOUND_DIR / rel
            if not sound_path.exists() or not sound_path.is_file():
                self.send_response(404); self.end_headers(); return
            self._serve_range_file(sound_path)

        elif path == "/api/export":
            # 色補正版(final_export.mp4)があればそれを、無ければ通常final.mp4を配信
            export_path = work_dir / "final_export.mp4"
            final_path = export_path if export_path.exists() else (work_dir / "final.mp4")
            if not final_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            size = final_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", 'attachment; filename="reel.mp4"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(final_path, "rb") as f:
                import shutil as _shutil
                _shutil.copyfileobj(f, self.wfile)

        else:
            self.send_response(404)
            self.end_headers()

    # ── POST ───────────────────────────────────────
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        data = json.loads(body) if body else {}

        if self.path == "/api/save":
            # 既存のトップレベルキー（voiceover_path / copy_mode / reference_video 等）は
            # 保持したまま cards だけ更新する。以前は {cards, voiceover_path} で丸ごと
            # 上書きしていたため、リール間コピーの copy_mode/reference_video が
            # 最初の自動保存で消えていた。
            existing = _load_json(work_dir / "timeline.json")
            existing["cards"] = data["cards"]
            _save_json(work_dir / "timeline.json", existing)
            self._send_json({"ok": True})

        elif self.path == "/api/reassign":
            self._handle_reassign(data)

        elif self.path == "/api/render":
            self._handle_render(data)

        elif self.path == "/api/export_render":
            self._handle_export_render(data)

        elif self.path == "/api/rescan":
            result = scan_and_register_materials()
            self._send_json(result)

        else:
            self.send_response(404)
            self.end_headers()

    # ── ハンドラ ───────────────────────────────────
    def _handle_reassign(self, data: dict):
        """指定カードについて、tag_filterに合うクリップを再選定する。
        素材は再利用可（他カードが使っていても候補にする）。同じ素材が他カードで
        既に使われている場合は、別のin点（別の瞬間）を割り当てて違うカットにする。"""
        card_id = data["card_id"]
        tag_filter = data.get("tag_filter", "either")

        timeline = _load_json(work_dir / "timeline.json")
        materials = _load_json(work_dir / "materials.json")
        cards = timeline["cards"]

        target = next((c for c in cards if c["id"] == card_id), None)
        if target is None:
            self._send_json({"ok": False, "error": "カードが見つかりません"})
            return

        candidates = materials["clips"]
        if not candidates:
            self._send_json({"ok": False, "error": "割当て可能な素材がありません"})
            return

        # 自動割当て（assign_clips._best_clip）と同じく、直前・直後のカードと
        # 同じ素材・同じ撮影（重複ファイル）は避ける（毎カット切り替わるように）。
        # 該当が無ければ制約を外して候補にする。
        target_idx = next((i for i, c in enumerate(cards) if c["id"] == card_id), None)
        avoid_files, avoid_sigs = set(), set()
        neighbor_indices = []
        if target_idx is not None:
            neighbor_indices = [target_idx - 1, target_idx + 1]
        for neighbor_idx in neighbor_indices:
            if not (0 <= neighbor_idx < len(cards)):
                continue
            for sub in get_clips(cards[neighbor_idx]):
                avoid_files.add(sub["file"])
                match = next((c for c in candidates if c["file"] == sub["file"]), None)
                if match:
                    avoid_sigs.add(_signature(match))

        keywords = [kw[0] for kw in detect_keywords(target["text"])]
        constrained = [c for c in candidates
                       if c["file"] not in avoid_files and _signature(c) not in avoid_sigs]
        pool = constrained or candidates
        best = max(pool, key=lambda c: _score(keywords, tag_filter, c))

        target_duration = target["end"] - target["start"]
        clip_duration = round(min(target_duration, best["duration"]), 3)
        # 同じ素材を使っている他カードのin点は避けて別の瞬間を選ぶ
        used_ins = set()
        for c in cards:
            if c["id"] == card_id:
                continue
            for sub in get_clips(c):
                if sub["file"] == best["file"]:
                    used_ins.add(round(sub["in"], 3))
        new_in = _pick_in_point_for_use(best, clip_duration, used_ins)
        new_clip = {"file": best["file"], "in": new_in, "duration": clip_duration}

        # タグ再選定はカードを単一クリップに戻す（素材ごと選び直す挙動）
        target["clips"] = [new_clip]
        target.pop("clip", None)
        target["tag_filter"] = tag_filter
        _save_json(work_dir / "timeline.json", timeline)

        self._send_json({"ok": True, "clips": target["clips"]})

    def _reference_video(self, timeline: dict):
        """リール間コピー用の参考リール(reference.mp4)のパス。未割当てカードを
        仮映像で埋めるために render_timeline に渡す。無ければ None（通常フロー）。"""
        ref_name = timeline.get("reference_video")
        if not ref_name:
            return None
        ref = work_dir / ref_name
        return ref if ref.exists() else None

    def _handle_render(self, data: dict):
        timeline = _load_json(work_dir / "timeline.json")
        voiceover_path = timeline.get("voiceover_path")
        jl_cut_offset = 0.2 if data.get("jl_cut") else 0.0

        try:
            render_timeline(timeline["cards"], materials_dir, work_dir / "final.mp4",
                             voiceover_path, jl_cut_offset,
                             reference_video=self._reference_video(timeline))
            self._send_json({"ok": True, "output": str(work_dir / "final.mp4")})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _handle_export_render(self, data: dict):
        """動画出力用のレンダー。HDR素材だけBT.709へ変換して色補正版(final_export.mp4)を
        作る（編集中の更新は変換なしで速いまま。ここだけ色補正）。"""
        timeline = _load_json(work_dir / "timeline.json")
        voiceover_path = timeline.get("voiceover_path")
        jl_cut_offset = 0.2 if data.get("jl_cut") else 0.0
        try:
            render_timeline(timeline["cards"], materials_dir, work_dir / "final_export.mp4",
                             voiceover_path, jl_cut_offset, hdr_fix=True,
                             reference_video=self._reference_video(timeline))
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _send_file(self, path: Path, content_type: str):
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_range_file(self, path: Path):
        """Rangeリクエスト対応の汎用ファイル配信（複数の素材クリップ・final.mp4で共用）"""
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return

        size = path.stat().st_size
        mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
        rng = self.headers.get("Range")

        if rng:
            raw = rng.replace("bytes=", "")
            parts = raw.split("-")
            if parts[0] == "":
                suffix_len = int(parts[1])
                start = max(0, size - suffix_len)
                end = size - 1
            else:
                start = int(parts[0])
                end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)


def main():
    global project, work_dir, materials_dir

    if len(sys.argv) < 2:
        print("使い方: python scripts/editor_server.py <project>")
        sys.exit(1)

    project = sys.argv[1]
    work_dir = WORK_ROOT / project

    timeline_path = work_dir / "timeline.json"
    materials_path = work_dir / "materials.json"
    if not timeline_path.exists() or not materials_path.exists():
        print(f"❌ {work_dir} に timeline.json / materials.json が見つかりません")
        print(f"   先に build_shorts.py を実行してください")
        sys.exit(1)

    materials_dir = Path(_load_json(materials_path)["materials_dir"])
    # 記録された素材フォルダが無い場合（リポジトリの移動・改名等）、素材を
    # work_dir内に置く構成ならそこから自動復旧する（voiceover・framesと同じ方針）。
    if not materials_dir.exists() and (work_dir / "materials").exists():
        materials_dir = work_dir / "materials"
        print(f"ℹ️ 素材フォルダを自動復旧: {materials_dir}")

    def _prewarm_proxies():
        # h264_videotoolbox（ハードウェアエンコーダ）は共有資源で、並列化すると
        # 互いに競合してかえって遅くなり、ユーザーがその場で別素材に切り替えた
        # ときのオンデマンド変換まで圧迫する。プリウォームは逐次で静かに回す。
        # タイムラインで実際に使われている素材を先に温める（既定表示の再生を速く）。
        clips = _load_json(materials_path).get("clips", [])
        try:
            used = set()
            for c in _load_json(timeline_path).get("cards", []):
                for cl in c.get("clips", []):
                    used.add(cl.get("file"))
            clips = sorted(clips, key=lambda c: 0 if c.get("file") in used else 1)
        except Exception:
            pass
        for clip in clips:
            try:
                _get_preview_proxy(clip["file"])
            except Exception:
                pass
        print(f"   ✅ プロキシ事前生成完了 ({len(clips)}件)")

    threading.Thread(target=_prewarm_proxies, daemon=True).start()

    url = f"http://localhost:{PORT}"
    print(f"\n🌐 エディタ起動中... {url}")
    print(f"   プロジェクト: {project}")
    print(f"\n   終了するには Ctrl+C\n")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 エディタを終了しました")


if __name__ == "__main__":
    main()
