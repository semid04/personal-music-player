#!/usr/bin/env python3
"""
Python Music Player (Tkinter + VLC) – Audio Only
-------------------------------------------------
Strictly audio-only: VLC started with video output disabled,
and file filters allow only common audio extensions.
"""

import os
import random
import traceback
from dataclasses import dataclass
from typing import List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import vlc
except ImportError:
    vlc = None

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:
    MutagenFile = None
    ID3 = None
    FLAC = None
    MP4 = None
    MP4Cover = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

# Pure audio extensions – no video containers
SUPPORTED_EXTS = {
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma", ".mka"
}
STATE_FILE = os.path.join(os.path.dirname(__file__), "player_state.json")


@dataclass
class Track:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    liked: bool = False

    def display_text(self) -> str:
        base = self.title or os.path.basename(self.path)
        artist = f" — {self.artist}" if self.artist else ""
        return f"{base}{artist}"


class MusicPlayerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Python Music Player")
        self.geometry("1000x660")
        self.minsize(920, 560)

        if vlc is None:
            messagebox.showerror("Missing dependency",
                                 "python-vlc is not installed.\n\nInstall with:\n  pip install python-vlc\n\nAlso ensure VLC is installed on your system.")
            self.destroy()
            return

        # VLC instance with video output disabled (audio only)
        self.instance = vlc.Instance('--no-video', '--vout=disable')
        self.player = self.instance.media_player_new()
        self.em = self.player.event_manager()

        # State
        self.playlist: List[Track] = []
        self.filtered_indices: List[int] = []   # indices into self.playlist after search filter
        self.current_index: int = -1
        self.shuffled: bool = False
        self.repeat_mode: str = "off"  # "off" | "one" | "all"
        self.muted: bool = False
        self.drag_from_vis: Optional[int] = None
        self.seeking_user: bool = False
        self.user_stopped: bool = False  # used to prevent advancing on manual Stop

        # UI
        self._make_style()
        self._build_ui()
        self._bind_shortcuts()

        # VLC event: advance on end (main-thread safe using after)
        try:
            self.em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end)
        except Exception:
            pass

        self.after(200, self._poll_position)
        self._load_state()

    # -------------------- UI --------------------
    def _make_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", padding=6)
        style.configure("Muted.TButton", foreground="#888")
        style.configure("Small.TLabel", font=("", 9))

    def _build_ui(self):
        # Top controls
        top = ttk.Frame(self, padding=(10,10))
        top.pack(side="top", fill="x")

        self.btn_prev = ttk.Button(top, text="⏮ Prev", command=self.prev_track)
        self.btn_playpause = ttk.Button(top, text="▶ Play", command=self.toggle_play)
        self.btn_next  = ttk.Button(top, text="⏭ Next", command=self.next_track)
        self.btn_stop = ttk.Button(top, text="⏹ Stop", command=self.stop_track)

        self.btn_prev.pack(side="left", padx=3)
        self.btn_playpause.pack(side="left", padx=3)
        self.btn_next.pack(side="left", padx=3)
        self.btn_stop.pack(side="left", padx=3)

        # Shuffle / Repeat / Like
        self.btn_shuffle = ttk.Button(top, text="🔀 Shuffle: Off", command=self.toggle_shuffle)
        self.btn_repeat = ttk.Button(top, text="↻ Repeat: Off", command=self.cycle_repeat)
        self.btn_like = ttk.Button(top, text="♡ Like", command=self.toggle_like, style="Muted.TButton")

        self.btn_shuffle.pack(side="left", padx=10)
        self.btn_repeat.pack(side="left", padx=3)
        self.btn_like.pack(side="left", padx=10)

        # Volume
        self.btn_mute = ttk.Button(top, text="🔊", width=3, command=self.toggle_mute)
        self.btn_mute.pack(side="right")
        self.vol_var = tk.DoubleVar(value=80)
        self.scale_vol = ttk.Scale(top, from_=0, to=100, orient="horizontal",
                                   variable=self.vol_var, command=self._on_volume)
        self.scale_vol.pack(side="right", padx=(0, 8), ipadx=60)

        # Search
        ttk.Label(top, text="Search:").pack(side="right", padx=(12,4))
        self.search_var = tk.StringVar()
        self.entry_search = ttk.Entry(top, textvariable=self.search_var, width=24)
        self.entry_search.pack(side="right")
        self.entry_search.bind("<KeyRelease>", lambda e: self._apply_filter())

        # Middle split
        mid = ttk.Frame(self, padding=(10,0))
        mid.pack(side="top", fill="both", expand=True)

        # Left: playlist
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        self.listbox = tk.Listbox(left, selectmode="browse", activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(0,6))
        # Single click now plays the selected track
        self.listbox.bind("<<ListboxSelect>>", self.play_selected)
        self.listbox.bind("<Double-1>", lambda e: self.play_selected())
        self.listbox.bind("<Button-1>", self._on_listbox_click)
        self.listbox.bind("<B1-Motion>", self._on_listbox_drag)
        self.listbox.bind("<ButtonRelease-1>", self._on_listbox_drop)

        sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        # Right: now playing
        right = ttk.Frame(mid, width=320)
        right.pack(side="left", fill="y")
        right.pack_propagate(False)

        self.cover_label = ttk.Label(right)
        self.cover_label.pack(pady=(8,4))

        self.lab_title = ttk.Label(right, text="Title: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_artist = ttk.Label(right, text="Artist: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_album = ttk.Label(right, text="Album: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_file = ttk.Label(right, text="File: —", style="Small.TLabel", wraplength=300, justify="left")

        self.lab_title.pack(anchor="w", padx=6, pady=2)
        self.lab_artist.pack(anchor="w", padx=6, pady=2)
        self.lab_album.pack(anchor="w", padx=6, pady=2)
        self.lab_file.pack(anchor="w", padx=6, pady=2)

        # Bottom: seekbar + time
        bottom = ttk.Frame(self, padding=(10,10))
        bottom.pack(side="bottom", fill="x")

        self.time_now = ttk.Label(bottom, text="0:00")
        self.time_total = ttk.Label(bottom, text="0:00")
        self.scale_pos = ttk.Scale(bottom, from_=0, to=1000, orient="horizontal",
                                   command=self._on_seek_command)
        self.scale_pos.bind("<ButtonPress-1>", lambda e: setattr(self, "seeking_user", True))
        self.scale_pos.bind("<ButtonRelease-1>", lambda e: setattr(self, "seeking_user", False))

        self.time_now.pack(side="left")
        self.scale_pos.pack(side="left", fill="x", expand=True, padx=8)
        self.time_total.pack(side="left")

        # Menu
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Open Files… (Ctrl+O)", command=self.add_files)
        m_file.add_command(label="Open Folder… (Ctrl+Shift+O)", command=self.add_folder)
        m_file.add_separator()
        m_file.add_command(label="Save Playlist… (Ctrl+S)", command=self.save_playlist)
        m_file.add_command(label="Load Playlist… (Ctrl+L)", command=self.load_playlist)
        m_file.add_separator()
        m_file.add_command(label="Quit", command=self.on_quit)
        menubar.add_cascade(label="File", menu=m_file)

        m_play = tk.Menu(menubar, tearoff=0)
        m_play.add_command(label="Play/Pause (space)", command=self.toggle_play)
        m_play.add_command(label="Next (N)", command=self.next_track)
        m_play.add_command(label="Previous (P)", command=self.prev_track)
        m_play.add_separator()
        m_play.add_command(label="Shuffle (S)", command=self.toggle_shuffle)
        m_play.add_command(label="Repeat (R)", command=self.cycle_repeat)
        menubar.add_cascade(label="Playback", menu=m_play)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=m_help)

    def _bind_shortcuts(self):
        # Helper to ignore event if focus is in an entry or text widget
        def ignore_in_entry(event):
            return isinstance(event.widget, (tk.Entry, tk.Text))

        # Playback controls – ignored when typing in search bar
        self.bind("<space>", lambda e: self.toggle_play() if not ignore_in_entry(e) else None)
        self.bind("<Left>", lambda e: self._nudge(-5) if not ignore_in_entry(e) else None)
        self.bind("<Right>", lambda e: self._nudge(5) if not ignore_in_entry(e) else None)
        self.bind("<Up>", lambda e: self._vol_nudge(5) if not ignore_in_entry(e) else None)
        self.bind("<Down>", lambda e: self._vol_nudge(-5) if not ignore_in_entry(e) else None)
        self.bind("<s>", lambda e: self.toggle_shuffle() if not ignore_in_entry(e) else None)
        self.bind("<r>", lambda e: self.cycle_repeat() if not ignore_in_entry(e) else None)
        self.bind("<n>", lambda e: self.next_track() if not ignore_in_entry(e) else None)
        self.bind("<p>", lambda e: self.prev_track() if not ignore_in_entry(e) else None)

        # File menu shortcuts (Ctrl combos) – safe because they use modifier
        self.bind("<Control-o>", lambda e: self.add_files())
        self.bind("<Control-O>", lambda e: self.add_folder())
        self.bind("<Delete>", lambda e: self.remove_selected())
        self.bind("<Control-s>", lambda e: self.save_playlist())
        self.bind("<Control-l>", lambda e: self.load_playlist())

    # -------------------- Playlist ops --------------------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio files", " ".join(f"*{ext}" for ext in SUPPORTED_EXTS))]
        )
        if not paths:
            return
        self._append_tracks(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select folder with audio")
        if not folder:
            return
        paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS:
                    paths.append(os.path.join(root, f))
        if not paths:
            messagebox.showinfo("No audio", "No supported audio files found.")
            return
        self._append_tracks(paths)

    def _append_tracks(self, paths):
        for p in paths:
            self.playlist.append(self._make_track(p))
        self._refresh_listbox()
        if self.current_index == -1 and self.playlist:
            self._play_index(0)

    def remove_selected(self):
        idx = self._selected_index_in_playlist()
        if idx is None:
            return
        removing_current = (idx == self.current_index)
        del self.playlist[idx]
        if not self.playlist:
            self.current_index = -1
            self.stop_track()
        else:
            if removing_current:
                next_idx = min(idx, len(self.playlist)-1)
                self.current_index = next_idx
                self._play_index(self.current_index)
            elif idx < self.current_index:
                self.current_index -= 1
        self._refresh_listbox()

    def _make_track(self, path: str) -> Track:
        t = Track(path=path)
        if MutagenFile is not None:
            try:
                mf = MutagenFile(path)
                if mf is not None:
                    if getattr(mf, "info", None) and hasattr(mf.info, "length"):
                        t.duration = float(mf.info.length)
                    tags = getattr(mf, "tags", None)
                    def get_tag(*keys):
                        if not tags:
                            return ""
                        for k in keys:
                            v = tags.get(k) if hasattr(tags, "get") else None
                            if v:
                                if isinstance(v, list):
                                    return str(v[0])
                                return str(v)
                            v = tags.get(k.lower()) if hasattr(tags, "get") else None
                            if v:
                                if isinstance(v, list):
                                    return str(v[0])
                                return str(v)
                        return ""

                    t.title = get_tag("TIT2","TITLE","\xa9nam","title") or os.path.basename(path)
                    t.artist = get_tag("TPE1","ARTIST", "\xa9ART","artist")
                    t.album = get_tag("TALB","ALBUM",  "\xa9alb","album")
            except Exception:
                traceback.print_exc()
        if not t.title:
            t.title = os.path.basename(path)
        return t

    def _refresh_listbox(self):
        query = self.search_var.get().strip().lower()
        self.listbox.delete(0, "end")
        self.filtered_indices.clear()
        for idx, tr in enumerate(self.playlist):
            text = tr.display_text()
            if query and query not in text.lower():
                continue
            self.listbox.insert("end", text)
            self.filtered_indices.append(idx)

        if 0 <= self.current_index < len(self.playlist):
            try:
                vis_index = self.filtered_indices.index(self.current_index)
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(vis_index)
                self.listbox.see(vis_index)
            except ValueError:
                self.listbox.selection_clear(0, "end")
        else:
            self.listbox.selection_clear(0, "end")

        self._update_now_playing_panel()

    def _apply_filter(self):
        self._refresh_listbox()

    def _selected_index_in_playlist(self) -> Optional[int]:
        sel = self.listbox.curselection()
        if not sel:
            return None
        vis_index = sel[0]
        if vis_index < 0 or vis_index >= len(self.filtered_indices):
            return None
        return self.filtered_indices[vis_index]

    def play_selected(self, event=None):
        idx = self._selected_index_in_playlist()
        if idx is None:
            return
        self._play_index(idx)

    # Drag-to-reorder handlers
    def _on_listbox_click(self, event):
        try:
            self.drag_from_vis = self.listbox.nearest(event.y)
        except Exception:
            self.drag_from_vis = None

    def _on_listbox_drag(self, event):
        if self.drag_from_vis is None:
            return
        i = self.listbox.nearest(event.y)
        if i == self.drag_from_vis or i < 0:
            return
        txt_from = self.listbox.get(self.drag_from_vis)
        txt_to = self.listbox.get(i)
        self.listbox.delete(i)
        self.listbox.insert(i, txt_from)
        self.listbox.delete(self.drag_from_vis)
        self.listbox.insert(self.drag_from_vis, txt_to)
        if self.drag_from_vis < len(self.filtered_indices) and i < len(self.filtered_indices):
            a = self.filtered_indices[self.drag_from_vis]
            b = self.filtered_indices[i]
            self.playlist[a], self.playlist[b] = self.playlist[b], self.playlist[a]
            if self.current_index == a:
                self.current_index = b
            elif self.current_index == b:
                self.current_index = a
        self.drag_from_vis = i

    def _on_listbox_drop(self, event):
        self.drag_from_vis = None
        self._refresh_listbox()

    # -------------------- Playback (VLC with video disabled) --------------------
    def _play_index(self, idx: int):
        if not (0 <= idx < len(self.playlist)):
            return
        self.current_index = idx
        tr = self.playlist[idx]

        # Extra safety: refuse to play if extension not supported
        if os.path.splitext(tr.path)[1].lower() not in SUPPORTED_EXTS:
            messagebox.showerror("Invalid file", f"File type not supported:\n{tr.path}")
            return

        media = self.instance.media_new(tr.path)
        self.player.set_media(media)
        self.player.play()
        self.user_stopped = False
        self._set_playing_ui(True)

        self.player.audio_set_volume(int(self.vol_var.get()))
        self.player.audio_set_mute(self.muted)

        self._update_now_playing_panel()
        self.after(300, self._update_total_time)
        self._refresh_listbox()

    def toggle_play(self):
        try:
            if self.player.is_playing():
                self.player.pause()
                self._set_playing_ui(False)
            else:
                if self.current_index == -1 and self.playlist:
                    self._play_index(0)
                else:
                    self.player.play()
                    self._set_playing_ui(True)
        except Exception:
            traceback.print_exc()

    def stop_track(self):
        self.user_stopped = True
        try:
            self.player.stop()
        except Exception:
            pass
        self._set_playing_ui(False)
        self.scale_pos.set(0)
        self.time_now.configure(text="0:00")

    def next_track(self):
        if not self.playlist:
            return
        if self.repeat_mode == "one":
            self._play_index(self.current_index)
            return
        if self.shuffled:
            choices = [i for i in range(len(self.playlist)) if i != self.current_index]
            if choices:
                nxt = random.choice(choices)
                self._play_index(nxt)
        else:
            nxt = self.current_index + 1
            if nxt >= len(self.playlist):
                if self.repeat_mode == "all":
                    nxt = 0
                else:
                    self.stop_track()
                    return
            self._play_index(nxt)

    def prev_track(self):
        if not self.playlist:
            return
        pos = max(self.player.get_time(), 0) / 1000.0
        if pos > 3:
            self.seek(0)
            return
        if self.shuffled:
            choices = [i for i in range(len(self.playlist)) if i != self.current_index]
            if choices:
                prv = random.choice(choices)
                self._play_index(prv)
        else:
            prv = self.current_index - 1
            if prv < 0:
                if self.repeat_mode == "all":
                    prv = len(self.playlist) - 1
                else:
                    self.stop_track()
                    return
            self._play_index(prv)

    def _set_playing_ui(self, playing: bool):
        self.btn_playpause.configure(text="⏸ Pause" if playing else "▶ Play")
        self.title(("▶ " if playing else "⏸ ") + "Python Music Player")

    def seek(self, seconds: float):
        try:
            self.player.set_time(int(seconds * 1000))
        except Exception:
            pass

    def _nudge(self, delta):
        total = max(self.player.get_length() / 1000.0, 0)
        now = max(self.player.get_time() / 1000.0, 0)
        self.seek(max(0, min(total, now + delta)))

    def _vol_nudge(self, delta):
        newv = max(0, min(100, self.vol_var.get() + delta))
        self.vol_var.set(newv)
        self._on_volume(str(newv))

    def _on_volume(self, _evt=None):
        v = int(float(self.vol_var.get()))
        try:
            self.player.audio_set_volume(v)
        except Exception:
            pass
        self.btn_mute.configure(text="🔇" if (self.muted or v == 0) else ("🔈" if v < 50 else "🔊"))

    def toggle_mute(self):
        self.muted = not self.muted
        try:
            self.player.audio_toggle_mute()
        except Exception:
            pass
        self.btn_mute.configure(text="🔇" if self.muted else ("🔈" if self.vol_var.get() < 50 else "🔊"))

    def toggle_shuffle(self):
        self.shuffled = not self.shuffled
        self.btn_shuffle.configure(text=f"🔀 Shuffle: {'On' if self.shuffled else 'Off'}")

    def cycle_repeat(self):
        self.repeat_mode = {"off":"one","one":"all","all":"off"}[self.repeat_mode]
        label = {"off":"Off","one":"One","all":"All"}[self.repeat_mode]
        self.btn_repeat.configure(text=f"↻ Repeat: {label}")

    def toggle_like(self):
        if not (0 <= self.current_index < len(self.playlist)):
            return
        tr = self.playlist[self.current_index]
        tr.liked = not tr.liked
        self.btn_like.configure(text="♥ Liked" if tr.liked else "♡ Like",
                                style=("" if tr.liked else "Muted.") + "TButton")
        self._refresh_listbox()

    # -------------------- Time/Seek UI --------------------
    def _on_seek_command(self, _evt=None):
        if not self.seeking_user:
            return
        total_ms = self.player.get_length()
        if total_ms and total_ms > 0:
            pos = float(self.scale_pos.get()) / 1000.0
            try:
                self.player.set_position(pos)
            except Exception:
                pass

    def _update_total_time(self):
        total_ms = self.player.get_length()
        if total_ms and total_ms > 0:
            tot = total_ms / 1000.0
            self.time_total.configure(text=self._fmt_time(tot))

    def _poll_position(self):
        try:
            total_ms = self.player.get_length()
            now_ms = self.player.get_time()
            if total_ms and total_ms > 0:
                pos = max(0.0, min(1.0, now_ms / total_ms))
                if not self.seeking_user:
                    self.scale_pos.set(int(pos * 1000))
                self.time_now.configure(text=self._fmt_time(max(0, now_ms/1000.0)))
                self.time_total.configure(text=self._fmt_time(max(0, total_ms/1000.0)))
            else:
                if not self.player.is_playing():
                    self.time_now.configure(text="0:00")
        except Exception:
            pass
        self.after(200, self._poll_position)

    # -------------------- VLC events --------------------
    def _on_vlc_end(self, event):
        self.after(0, self._on_media_end_mainthread)

    def _on_media_end_mainthread(self):
        if self.user_stopped:
            return
        if not self.playlist:
            return
        if self.repeat_mode == "one":
            self._play_index(self.current_index)
            return
        if self.shuffled:
            choices = [i for i in range(len(self.playlist)) if i != self.current_index]
            if choices:
                nxt = random.choice(choices)
                self._play_index(nxt)
        else:
            nxt = self.current_index + 1
            if nxt >= len(self.playlist):
                if self.repeat_mode == "all":
                    nxt = 0
                else:
                    self.stop_track()
                    return
            self._play_index(nxt)

    # -------------------- Metadata & Art --------------------
    def _update_now_playing_panel(self):
        if not (0 <= self.current_index < len(self.playlist)):
            self.lab_title.configure(text="Title: —")
            self.lab_artist.configure(text="Artist: —")
            self.lab_album.configure(text="Album: —")
            self.lab_file.configure(text="File: —")
            self.cover_label.configure(image="", text="")
            return

        tr = self.playlist[self.current_index]
        self.lab_title.configure(text=f"Title: {tr.title or os.path.basename(tr.path)}")
        self.lab_artist.configure(text=f"Artist: {tr.artist or '—'}")
        self.lab_album.configure(text=f"Album: {tr.album or '—'}")
        self.lab_file.configure(text=f"File: {tr.path}")

        self.btn_like.configure(text="♥ Liked" if tr.liked else "♡ Like",
                                style=("" if tr.liked else "Muted.") + "TButton")

        if Image is not None:
            img = self._extract_cover_image(tr.path)
            if img is not None:
                try:
                    img.thumbnail((280, 280))
                    self._cover_photo = ImageTk.PhotoImage(img)
                    self.cover_label.configure(image=self._cover_photo, text="")
                except Exception:
                    self.cover_label.configure(image="", text="")
            else:
                self.cover_label.configure(image="", text="")

    def _extract_cover_image(self, path):
        if MutagenFile is not None and Image is not None:
            try:
                ext = os.path.splitext(path)[1].lower()
                if ext in (".mp3",):
                    mf = MutagenFile(path, easy=False)
                    if hasattr(mf, "tags") and mf.tags:
                        apics = []
                        try:
                            apics = mf.tags.getall("APIC")
                        except Exception:
                            apics = []
                        if apics:
                            from io import BytesIO
                            try:
                                return Image.open(BytesIO(apics[0].data))
                            except Exception:
                                pass
                elif ext in (".flac",):
                    if FLAC is not None:
                        fl = FLAC(path)
                        if getattr(fl, "pictures", None):
                            from io import BytesIO
                            try:
                                return Image.open(BytesIO(fl.pictures[0].data))
                            except Exception:
                                pass
                elif ext in (".m4a",".mp4"):
                    if MP4 is not None:
                        mp = MP4(path)
                        covr = mp.tags.get("covr") if mp.tags else None
                        if covr:
                            from io import BytesIO
                            try:
                                data = covr[0]
                                if hasattr(data, "data"):
                                    data = data.data
                                return Image.open(BytesIO(data))
                            except Exception:
                                pass
                else:
                    mf = MutagenFile(path, easy=False)
                    if mf is not None and getattr(mf, "tags", None):
                        for key in ("APIC:", "APIC", "METADATA_BLOCK_PICTURE"):
                            pic = mf.tags.get(key)
                            if pic:
                                from io import BytesIO
                                blob = getattr(pic, "data", None) or (pic[0].data if isinstance(pic, list) else None)
                                if blob:
                                    try:
                                        return Image.open(BytesIO(blob))
                                    except Exception:
                                        pass
            except Exception:
                pass

        folder = os.path.dirname(path) or "."
        for name in ("cover.jpg","cover.png","folder.jpg","folder.png","AlbumArtSmall.jpg"):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and Image is not None:
                try:
                    return Image.open(p)
                except Exception:
                    pass
        return None

    # -------------------- Save / Load --------------------
    def save_playlist(self):
        if not self.playlist:
            messagebox.showinfo("Nothing to save", "Add some tracks first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save playlist as M3U8",
            defaultextension=".m3u8",
            filetypes=[("M3U8 playlist",".m3u8"), ("All files","*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for t in self.playlist:
                    dur = int(t.duration) if t.duration else -1
                    title = t.title or os.path.basename(t.path)
                    f.write(f"#EXTINF:{dur},{title}\n")
                    f.write(t.path + "\n")
            messagebox.showinfo("Saved", f"Playlist saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save playlist:\n{e}")

    def load_playlist(self):
        path = filedialog.askopenfilename(
            title="Load M3U or M3U8",
            filetypes=[("M3U/M3U8", "*.m3u *.m3u8"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            new_paths = []
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if os.path.isfile(line) and os.path.splitext(line)[1].lower() in SUPPORTED_EXTS:
                        new_paths.append(line)
            if not new_paths:
                messagebox.showinfo("Empty", "No valid audio file paths found in playlist.")
                return
            self.playlist.clear()
            self._append_tracks(new_paths)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load playlist:\n{e}")

    # -------------------- Persist --------------------
    def _load_state(self):
        try:
            if not os.path.isfile(STATE_FILE):
                return
            import json
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            self.shuffled = bool(st.get("shuffle", False))
            self.repeat_mode = st.get("repeat", "off")
            vol = int(st.get("volume", 80))
            self.vol_var.set(vol)
            self.player.audio_set_volume(vol)
            self.muted = bool(st.get("muted", False))
            self.player.audio_set_mute(self.muted)
            self.btn_shuffle.configure(text=f"🔀 Shuffle: {'On' if self.shuffled else 'Off'}")
            self.btn_repeat.configure(text=f"↻ Repeat: { {'off':'Off','one':'One','all':'All'}[self.repeat_mode] }")
            paths = st.get("paths", [])
            keep = [p for p in paths if os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXTS]
            if keep:
                self._append_tracks(keep)
            idx = int(st.get("current_index", -1))
            if 0 <= idx < len(self.playlist):
                self.current_index = idx
                self._refresh_listbox()
        except Exception:
            traceback.print_exc()

    def on_quit(self):
        try:
            st = {
                "shuffle": self.shuffled,
                "repeat": self.repeat_mode,
                "volume": int(self.vol_var.get()),
                "muted": self.muted,
                "paths": [t.path for t in self.playlist],
                "current_index": self.current_index,
            }
            import json
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=2)
        except Exception:
            pass
        self.destroy()

    # -------------------- Misc --------------------
    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"

    def _about(self):
        messagebox.showinfo(
            "About",
            "Python Music Player\n\nGUI: Tkinter\nPlayback: VLC (audio only)\nTags: mutagen\nArt: Pillow\n\nStrictly audio – no video."
        )


def main():
    app = MusicPlayerApp()
    app.protocol("WM_DELETE_WINDOW", app.on_quit)
    app.mainloop()


if __name__ == "__main__":
    main()