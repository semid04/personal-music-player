#!/usr/bin/env python3
"""
Python Music Player (Tkinter + VLC + Telegram)
-----------------------------------
Robust music player with local files and Telegram channel support.

Key Features:
- Local file playback (MP3, FLAC, etc.)
- Telegram channel audio streaming
- Playlist management, search, shuffle/repeat
- Album art extraction
- M3U8 save/load
- Keyboard shortcuts

Dependencies:
  python-vlc, mutagen, Pillow, telethon
"""

import os
import sys
import random
import traceback
import asyncio
import threading
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional / external deps
try:
    import vlc
except Exception:
    vlc = None

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4, MP4Cover
except Exception:
    MutagenFile = None
    ID3 = None
    FLAC = None
    MP4 = None
    MP4Cover = None

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

try:
    from telethon import TelegramClient
    from telethon.tl.types import (
        MessageMediaDocument, Document, DocumentAttributeAudio,
        DocumentAttributeFilename, Message
    )
    from telethon.tl.functions.messages import GetHistoryRequest
    from telethon.errors import (
        SessionPasswordNeededError, PhoneNumberInvalidError,
        FloodWaitError, ChannelInvalidError
    )

    TELETHON_AVAILABLE = True
except Exception:
    TELETHON_AVAILABLE = False

SUPPORTED_EXTS = {
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma", ".mka", ".mkv", ".mp4"
}
STATE_FILE = os.path.join(os.path.dirname(__file__), "player_state.json")
TELEGRAM_SESSION_FILE = os.path.join(os.path.dirname(__file__), "telegram_session")
TELEGRAM_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "telegram_config.json")


@dataclass
class Track:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    liked: bool = False
    source: str = "local"  # "local" or "telegram"
    telegram_file_id: Optional[int] = None
    telegram_access_hash: Optional[int] = None
    telegram_file_reference: Optional[bytes] = None
    file_size: int = 0

    def display_text(self) -> str:
        base = self.title or os.path.basename(self.path)
        artist = f" — {self.artist}" if self.artist else ""
        source_indicator = " 📡" if self.source == "telegram" else ""
        return f"{base}{artist}{source_indicator}"


class TelegramManager:
    def __init__(self, app):
        self.app = app
        self.client: Optional[TelegramClient] = None
        self.connected = False
        self.download_queue = asyncio.Queue()
        self.download_cache_dir = os.path.join(os.path.dirname(__file__), "telegram_cache")
        os.makedirs(self.download_cache_dir, exist_ok=True)

        # Load config
        self.api_id = None
        self.api_hash = None
        self._load_config()

    def _load_config(self):
        """Load Telegram API configuration"""
        try:
            import json
            if os.path.exists(TELEGRAM_CONFIG_FILE):
                with open(TELEGRAM_CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.api_id = config.get('api_id')
                    self.api_hash = config.get('api_hash')
        except Exception:
            pass

    def save_config(self, api_id: str, api_hash: str):
        """Save Telegram API configuration"""
        try:
            import json
            with open(TELEGRAM_CONFIG_FILE, 'w') as f:
                json.dump({'api_id': api_id, 'api_hash': api_hash}, f)
            self.api_id = api_id
            self.api_hash = api_hash
            return True
        except Exception as e:
            messagebox.showerror("Config Error", f"Failed to save config: {e}")
            return False

    async def _async_connect(self, phone: str, code_callback):
        """Async connection handler"""
        try:
            if not self.api_id or not self.api_hash:
                raise ValueError("API credentials not configured")

            self.client = TelegramClient(
                TELEGRAM_SESSION_FILE,
                int(self.api_id),
                self.api_hash
            )

            await self.client.start(phone=phone, code_callback=code_callback)
            self.connected = True
            return True
        except SessionPasswordNeededError:
            return "2FA"
        except PhoneNumberInvalidError:
            return "INVALID_PHONE"
        except FloodWaitError as e:
            return f"FLOOD_WAIT:{e.seconds}"
        except Exception as e:
            return f"ERROR:{str(e)}"

    def connect(self, phone: str, progress_callback):
        """Connect to Telegram (runs in thread)"""
        code = None
        password = None

        def code_callback():
            nonlocal code
            # This will be called from the async thread
            progress_callback("code")
            while code is None:
                import time
                time.sleep(0.1)
            return code

        async def run_connection():
            nonlocal code, password
            result = await self._async_connect(phone, code_callback)

            if result == "2FA":
                progress_callback("2fa")
                while password is None:
                    await asyncio.sleep(0.1)
                try:
                    await self.client.sign_in(password=password)
                    self.connected = True
                    progress_callback("success")
                except Exception as e:
                    progress_callback(f"error:{e}")
            elif result == True:
                progress_callback("success")
            else:
                progress_callback(result)

        # Run in event loop
        asyncio.run(run_connection())

    def set_code(self, code: str):
        """Set verification code"""
        self._code = code

    def set_password(self, password: str):
        """Set 2FA password"""
        self._password = password

    async def get_channel_audio(self, channel_identifier: str, progress_callback=None):
        """Get audio files from a Telegram channel"""
        if not self.client or not self.connected:
            raise ValueError("Not connected to Telegram")

        try:
            entity = await self.client.get_entity(channel_identifier)
            messages = await self.client.get_messages(entity, limit=1000)

            audio_tracks = []
            total = len(messages)

            for i, message in enumerate(messages):
                if progress_callback and i % 10 == 0:
                    progress_callback(i, total)

                if message.media and isinstance(message.media, MessageMediaDocument):
                    document = message.media.document
                    if isinstance(document, Document):
                        # Check if it's an audio file
                        audio_attr = None
                        filename_attr = None

                        for attr in document.attributes:
                            if isinstance(attr, DocumentAttributeAudio):
                                audio_attr = attr
                            elif isinstance(attr, DocumentAttributeFilename):
                                filename_attr = attr

                        if audio_attr and filename_attr:
                            # Create track
                            ext = os.path.splitext(filename_attr.file_name)[1].lower()
                            if ext in SUPPORTED_EXTS:
                                track = Track(
                                    path=filename_attr.file_name,
                                    title=audio_attr.title or filename_attr.file_name,
                                    artist=audio_attr.performer or "",
                                    duration=audio_attr.duration or 0.0,
                                    source="telegram",
                                    telegram_file_id=document.id,
                                    telegram_access_hash=document.access_hash,
                                    file_size=document.size
                                )
                                audio_tracks.append(track)

            if progress_callback:
                progress_callback(total, total)

            return audio_tracks, entity

        except ChannelInvalidError:
            raise ValueError("Channel not found or inaccessible")
        except Exception as e:
            raise Exception(f"Failed to get channel audio: {e}")

    async def download_audio_file(self, track: Track, progress_callback=None):
        """Download audio file from Telegram"""
        if not self.client:
            raise ValueError("Not connected to Telegram")

        cache_path = os.path.join(self.download_cache_dir, f"{track.telegram_file_id}.tmp")
        final_path = os.path.join(self.download_cache_dir, f"{track.telegram_file_id}{os.path.splitext(track.path)[1]}")

        # Check if already cached
        if os.path.exists(final_path):
            track.path = final_path
            return final_path

        try:
            # Download file
            await self.client.download_media(
                track,
                file=cache_path,
                progress_callback=progress_callback
            )

            # Rename to final name
            os.rename(cache_path, final_path)
            track.path = final_path
            return final_path

        except Exception as e:
            # Clean up failed download
            if os.path.exists(cache_path):
                os.remove(cache_path)
            raise e

    def disconnect(self):
        """Disconnect from Telegram"""
        if self.client:
            self.client.disconnect()
            self.client = None
        self.connected = False


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

        # VLC
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self.em = self.player.event_manager()

        # State
        self.playlist: List[Track] = []
        self.filtered_indices: List[int] = []  # indices into self.playlist after search filter
        self.current_index: int = -1
        self.shuffled: bool = False
        self.repeat_mode: str = "off"  # "off" | "one" | "all"
        self.muted: bool = False
        self.drag_from_vis: Optional[int] = None
        self.seeking_user: bool = False
        self.user_stopped: bool = False  # used to prevent advancing on manual Stop

        # Telegram
        self.telegram_manager = TelegramManager(self)
        self.telegram_connect_window = None
        self.telegram_channel_window = None
        self.current_channel_info = None

        # UI
        self._make_style()
        self._build_ui()
        self._bind_shortcuts()

        # VLC event: advance on end (main-thread safe using after)
        try:
            self.em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end)
        except Exception:
            # Fallback will be handled by polling if needed
            pass

        # Timers (UI-only; no playback calls here)
        self.after(200, self._poll_position)

        # Restore state
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
        style.configure("Telegram.TLabel", foreground="#0088cc")

    def _build_ui(self):
        # Top controls
        top = ttk.Frame(self, padding=(10, 10))
        top.pack(side="top", fill="x")

        self.btn_prev = ttk.Button(top, text="⏮ Prev", command=self.prev_track)
        self.btn_playpause = ttk.Button(top, text="▶ Play", command=self.toggle_play)
        self.btn_next = ttk.Button(top, text="⏭ Next", command=self.next_track)
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

        # Telegram status
        self.telegram_status = ttk.Label(top, text="📡 Disconnected", style="Muted.TLabel")
        self.telegram_status.pack(side="left", padx=10)

        # Volume
        self.btn_mute = ttk.Button(top, text="🔊", width=3, command=self.toggle_mute)
        self.btn_mute.pack(side="right")
        self.vol_var = tk.DoubleVar(value=80)
        self.scale_vol = ttk.Scale(top, from_=0, to=100, orient="horizontal",
                                   variable=self.vol_var, command=self._on_volume)
        self.scale_vol.pack(side="right", padx=(0, 8), ipadx=60)

        # Search
        ttk.Label(top, text="Search:").pack(side="right", padx=(12, 4))
        self.search_var = tk.StringVar()
        self.entry_search = ttk.Entry(top, textvariable=self.search_var, width=24)
        self.entry_search.pack(side="right")
        self.entry_search.bind("<KeyRelease>", lambda e: self._apply_filter())

        # Middle split
        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(side="top", fill="both", expand=True)

        # Left: playlist
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        self.listbox = tk.Listbox(left, selectmode="browse", activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.listbox.bind("<<ListboxSelect>>", self._on_select_track)
        self.listbox.bind("<Double-1>", lambda e: self.play_selected())
        # Drag-to-reorder (works within filtered view and updates underlying playlist)
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

        # Channel info panel
        self.channel_frame = ttk.LabelFrame(right, text="Channel Info", padding=5)
        self.channel_frame.pack(fill="x", padx=6, pady=(8, 4))

        self.channel_name = ttk.Label(self.channel_frame, text="—", style="Small.TLabel")
        self.channel_name.pack(anchor="w")

        self.channel_tracks = ttk.Label(self.channel_frame, text="Tracks: —", style="Small.TLabel")
        self.channel_tracks.pack(anchor="w")

        self.cover_label = ttk.Label(right)
        self.cover_label.pack(pady=(8, 4))

        self.lab_title = ttk.Label(right, text="Title: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_artist = ttk.Label(right, text="Artist: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_album = ttk.Label(right, text="Album: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_file = ttk.Label(right, text="File: —", style="Small.TLabel", wraplength=300, justify="left")
        self.lab_source = ttk.Label(right, text="Source: —", style="Small.TLabel", wraplength=300, justify="left")

        self.lab_title.pack(anchor="w", padx=6, pady=2)
        self.lab_artist.pack(anchor="w", padx=6, pady=2)
        self.lab_album.pack(anchor="w", padx=6, pady=2)
        self.lab_file.pack(anchor="w", padx=6, pady=2)
        self.lab_source.pack(anchor="w", padx=6, pady=2)

        # Bottom: seekbar + time
        bottom = ttk.Frame(self, padding=(10, 10))
        bottom.pack(side="bottom", fill="x")

        self.time_now = ttk.Label(bottom, text="0:00")
        self.time_total = ttk.Label(bottom, text="0:00")
        self.scale_pos = ttk.Scale(bottom, from_=0, to=1000, orient="horizontal",
                                   command=self._on_seek_command)
        # Grab/release events for smoother seeking
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
        if TELETHON_AVAILABLE:
            m_file.add_command(label="Load Telegram Channel… (Ctrl+T)", command=self.load_telegram_channel)
        m_file.add_separator()
        m_file.add_command(label="Quit", command=self.on_quit)
        menubar.add_cascade(label="File", menu=m_file)

        if TELETHON_AVAILABLE:
            m_telegram = tk.Menu(menubar, tearoff=0)
            m_telegram.add_command(label="Connect to Telegram…", command=self.connect_telegram)
            m_telegram.add_command(label="Disconnect from Telegram", command=self.disconnect_telegram)
            m_telegram.add_separator()
            m_telegram.add_command(label="Configure API…", command=self.configure_telegram_api)
            menubar.add_cascade(label="Telegram", menu=m_telegram)

        m_play = tk.Menu(menubar, tearoff=0)
        m_play.add_command(label="Play/Pause (Space)", command=self.toggle_play)
        m_play.add_command(label="Next (N)", command=self.next_track)
        m_play.add_command(label="Previous (P)", command=self.prev_track)
        m_play.add_separator()
        m_play.add_command(label="Shuffle (S)", command=self.toggle_shuffle)
        m_play.add_command(label="Repeat (R)", command=self.cycle_repeat)
        menubar.add_cascade(label="Playback", menu=m_play)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=m_help)

        # Update Telegram status
        self.update_telegram_status()

    def _bind_shortcuts(self):
        self.bind("<space>", lambda e: self.toggle_play())
        self.bind("<Left>", lambda e: self._nudge(-5))
        self.bind("<Right>", lambda e: self._nudge(5))
        self.bind("<Up>", lambda e: self._vol_nudge(5))
        self.bind("<Down>", lambda e: self._vol_nudge(-5))
        self.bind("<Control-o>", lambda e: self.add_files())
        self.bind("<Control-O>", lambda e: self.add_folder())
        self.bind("<Delete>", lambda e: self.remove_selected())
        self.bind("<Control-s>", lambda e: self.save_playlist())
        self.bind("<Control-l>", lambda e: self.load_playlist())
        if TELETHON_AVAILABLE:
            self.bind("<Control-t>", lambda e: self.load_telegram_channel())
        self.bind("<s>", lambda e: self.toggle_shuffle())
        self.bind("<r>", lambda e: self.cycle_repeat())
        self.bind("<n>", lambda e: self.next_track())
        self.bind("<p>", lambda e: self.prev_track())

    # -------------------- Telegram Integration --------------------
    def update_telegram_status(self):
        """Update Telegram connection status in UI"""
        if self.telegram_manager.connected:
            self.telegram_status.configure(text="📡 Connected", style="Telegram.TLabel")
        else:
            self.telegram_status.configure(text="📡 Disconnected", style="Muted.TLabel")

    def configure_telegram_api(self):
        """Configure Telegram API credentials"""
        dialog = tk.Toplevel(self)
        dialog.title("Configure Telegram API")
        dialog.geometry("400x200")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Telegram API Configuration", font=("", 12, "bold")).pack(pady=10)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="API ID:").grid(row=0, column=0, sticky="w", pady=5)
        api_id_var = tk.StringVar(value=self.telegram_manager.api_id or "")
        api_id_entry = ttk.Entry(frame, textvariable=api_id_var, width=30)
        api_id_entry.grid(row=0, column=1, sticky="ew", pady=5, padx=5)

        ttk.Label(frame, text="API Hash:").grid(row=1, column=0, sticky="w", pady=5)
        api_hash_var = tk.StringVar(value=self.telegram_manager.api_hash or "")
        api_hash_entry = ttk.Entry(frame, textvariable=api_hash_var, width=30)
        api_hash_entry.grid(row=1, column=1, sticky="ew", pady=5, padx=5)

        ttk.Label(frame, text="Get API credentials from: https://my.telegram.org/apps",
                  style="Small.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=10)

        def save_config():
            if self.telegram_manager.save_config(api_id_var.get(), api_hash_var.get()):
                dialog.destroy()
                messagebox.showinfo("Success", "Telegram API configuration saved!")

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)

        ttk.Button(btn_frame, text="Save", command=save_config).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side="left", padx=5)

    def connect_telegram(self):
        """Connect to Telegram"""
        if not TELETHON_AVAILABLE:
            messagebox.showerror("Telethon Not Available",
                                 "Telethon library is not installed.\n\nInstall with:\n  pip install telethon")
            return

        if not self.telegram_manager.api_id or not self.telegram_manager.api_hash:
            messagebox.showinfo("API Configuration Needed",
                                "Please configure Telegram API credentials first.")
            self.configure_telegram_api()
            return

        dialog = tk.Toplevel(self)
        dialog.title("Connect to Telegram")
        dialog.geometry("400x300")
        dialog.transient(self)
        dialog.grab_set()

        current_step = tk.StringVar(value="phone")
        status_text = tk.StringVar(value="Enter your phone number:")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        status_label = ttk.Label(frame, textvariable=status_text)
        status_label.pack(pady=10)

        input_var = tk.StringVar()
        input_entry = ttk.Entry(frame, textvariable=input_var, width=30)
        input_entry.pack(pady=10)

        progress = ttk.Progressbar(frame, mode='indeterminate')

        result_text = tk.StringVar()
        result_label = ttk.Label(frame, textvariable=result_text, style="Small.TLabel")

        def update_ui(state, message=None):
            if state == "phone":
                status_text.set("Enter your phone number (with country code):")
                input_entry.delete(0, tk.END)
                input_entry.pack(pady=10)
                progress.pack_forget()
                result_label.pack_forget()
            elif state == "code":
                status_text.set("Enter verification code:")
                input_entry.delete(0, tk.END)
                input_entry.pack(pady=10)
                progress.pack_forget()
            elif state == "2fa":
                status_text.set("Enter 2FA password:")
                input_entry.delete(0, tk.END)
                input_entry.pack(pady=10)
                progress.pack_forget()
            elif state == "connecting":
                status_text.set("Connecting to Telegram...")
                input_entry.pack_forget()
                progress.pack(pady=10)
                progress.start()
            elif state == "success":
                progress.stop()
                progress.pack_forget()
                status_text.set("Successfully connected!")
                result_text.set("You can now load Telegram channels.")
                result_label.pack(pady=10)
                self.after(2000, dialog.destroy)
                self.update_telegram_status()
            elif state == "error":
                progress.stop()
                progress.pack_forget()
                status_text.set("Connection failed")
                result_text.set(message or "Unknown error")
                result_label.pack(pady=10)

        def do_connect():
            phone = input_var.get()
            if not phone:
                return

            update_ui("connecting")

            # Run connection in thread
            def connection_thread():
                self.telegram_manager.connect(phone, update_ui)

            threading.Thread(target=connection_thread, daemon=True).start()

        def submit_input():
            if current_step.get() == "phone":
                do_connect()
            elif current_step.get() == "code":
                self.telegram_manager.set_code(input_var.get())
            elif current_step.get() == "2fa":
                self.telegram_manager.set_password(input_var.get())

        submit_btn = ttk.Button(frame, text="Submit", command=submit_input)
        submit_btn.pack(pady=10)

        input_entry.bind("<Return>", lambda e: submit_input())

    def disconnect_telegram(self):
        """Disconnect from Telegram"""
        self.telegram_manager.disconnect()
        self.update_telegram_status()
        messagebox.showinfo("Disconnected", "Disconnected from Telegram.")

    def load_telegram_channel(self):
        """Load audio from Telegram channel"""
        if not self.telegram_manager.connected:
            messagebox.showinfo("Not Connected", "Please connect to Telegram first.")
            self.connect_telegram()
            return

        dialog = tk.Toplevel(self)
        dialog.title("Load Telegram Channel")
        dialog.geometry("500x400")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Load Telegram Channel", font=("", 12, "bold")).pack(pady=10)

        content_frame = ttk.Frame(dialog, padding=20)
        content_frame.pack(fill="both", expand=True)

        ttk.Label(content_frame, text="Channel username or URL:").pack(anchor="w", pady=5)
        channel_var = tk.StringVar()
        channel_entry = ttk.Entry(content_frame, textvariable=channel_var, width=40)
        channel_entry.pack(fill="x", pady=5)

        # Progress area
        progress_frame = ttk.Frame(content_frame)
        progress_frame.pack(fill="x", pady=10)

        progress_label = ttk.Label(progress_frame, text="Ready")
        progress_label.pack(anchor="w")

        progress_bar = ttk.Progressbar(progress_frame, mode='determinate')
        progress_bar.pack(fill="x", pady=5)

        results_frame = ttk.Frame(content_frame)
        results_frame.pack(fill="both", expand=True)

        results_text = tk.Text(results_frame, height=10, wrap="word")
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=results_text.yview)
        results_text.configure(yscrollcommand=scrollbar.set)
        results_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def scan_channel():
            channel_id = channel_var.get().strip()
            if not channel_id:
                messagebox.showwarning("Input Needed", "Please enter a channel username or URL.")
                return

            progress_bar["value"] = 0
            results_text.delete(1.0, tk.END)

            def progress_callback(current, total):
                if total > 0:
                    percent = (current / total) * 100
                    dialog.after(0, lambda: progress_bar.config(value=percent))
                    dialog.after(0, lambda: progress_label.config(
                        text=f"Scanning... {current}/{total} messages ({percent:.1f}%)"
                    ))

            def do_scan():
                try:
                    async def async_scan():
                        return await self.telegram_manager.get_channel_audio(
                            channel_id,
                            progress_callback
                        )

                    # Run async function in thread
                    import asyncio
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    tracks, entity = loop.run_until_complete(async_scan())
                    loop.close()

                    dialog.after(0, lambda: on_scan_complete(tracks, entity))

                except Exception as e:
                    dialog.after(0, lambda: on_scan_error(str(e)))

            def on_scan_complete(tracks, entity):
                progress_label.config(text=f"Found {len(tracks)} audio tracks")

                # Add to playlist
                self.playlist.extend(tracks)
                self._refresh_listbox()

                # Update channel info
                self.current_channel_info = {
                    'name': getattr(entity, 'title', 'Unknown'),
                    'tracks': len(tracks)
                }
                self.update_channel_info()

                # Show results
                results_text.insert(tk.END, f"Successfully loaded {len(tracks)} tracks from:\n")
                results_text.insert(tk.END, f"Channel: {getattr(entity, 'title', 'Unknown')}\n")
                results_text.insert(tk.END, f"Tracks found: {len(tracks)}\n\n")

                for track in tracks[:10]:  # Show first 10 tracks
                    results_text.insert(tk.END, f"• {track.display_text()}\n")

                if len(tracks) > 10:
                    results_text.insert(tk.END, f"... and {len(tracks) - 10} more tracks\n")

            def on_scan_error(error_msg):
                progress_label.config(text="Error scanning channel")
                results_text.insert(tk.END, f"Error: {error_msg}\n")
                messagebox.showerror("Scan Error", f"Failed to scan channel:\n{error_msg}")

            # Start scanning in thread
            threading.Thread(target=do_scan, daemon=True).start()

        def close_dialog():
            dialog.destroy()

        btn_frame = ttk.Frame(content_frame)
        btn_frame.pack(fill="x", pady=10)

        ttk.Button(btn_frame, text="Scan Channel", command=scan_channel).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Close", command=close_dialog).pack(side="left", padx=5)

    def update_channel_info(self):
        """Update channel information panel"""
        if self.current_channel_info:
            self.channel_name.config(text=self.current_channel_info['name'])
            self.channel_tracks.config(text=f"Tracks: {self.current_channel_info['tracks']}")
        else:
            self.channel_name.config(text="—")
            self.channel_tracks.config(text="Tracks: —")

    # -------------------- Playlist ops --------------------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio files", " ".join(f"*{ext}" for ext in SUPPORTED_EXTS)),
                       ("All files", "*.*")]
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
        # If currently playing is being removed
        removing_current = (idx == self.current_index)
        del self.playlist[idx]
        if not self.playlist:
            self.current_index = -1
            self.stop_track()
        else:
            if removing_current:
                # Try to keep playing the next logical track
                next_idx = min(idx, len(self.playlist) - 1)
                self.current_index = next_idx
                self._play_index(self.current_index)
            elif idx < self.current_index:
                # Shift current index left
                self.current_index -= 1
        self._refresh_listbox()

    def _make_track(self, path: str) -> Track:
        t = Track(path=path)
        if MutagenFile is not None:
            try:
                mf = MutagenFile(path)
                if mf is not None:
                    # duration
                    if getattr(mf, "info", None) and hasattr(mf.info, "length"):
                        t.duration = float(mf.info.length)
                    # tags (try common keys, case-insensitive)
                    tags = getattr(mf, "tags", None)

                    def get_tag(*keys):
                        if not tags:
                            return ""
                        for k in keys:
                            # direct
                            v = tags.get(k) if hasattr(tags, "get") else None
                            if v:
                                if isinstance(v, list):
                                    return str(v[0])
                                return str(v)
                            # lowercase
                            v = tags.get(k.lower()) if hasattr(tags, "get") else None
                            if v:
                                if isinstance(v, list):
                                    return str(v[0])
                                return str(v)
                        return ""

                    # ID3 frames or Vorbis/FLAC/MP4 atoms
                    t.title = get_tag("TIT2", "TITLE", "\xa9nam", "title") or os.path.basename(path)
                    t.artist = get_tag("TPE1", "ARTIST", "\xa9ART", "artist")
                    t.album = get_tag("TALB", "ALBUM", "\xa9alb", "album")
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

        # Highlight current
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

    def _on_select_track(self, event=None):
        pass  # highlight only; double-click plays

    def play_selected(self):
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
        # Swap in UI
        txt_from = self.listbox.get(self.drag_from_vis)
        txt_to = self.listbox.get(i)
        self.listbox.delete(i)
        self.listbox.insert(i, txt_from)
        self.listbox.delete(self.drag_from_vis)
        self.listbox.insert(self.drag_from_vis, txt_to)
        # Swap in model via filtered indices
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

    # -------------------- Playback --------------------
    def _play_index(self, idx: int):
        if not (0 <= idx < len(self.playlist)):
            return
        self.current_index = idx
        tr = self.playlist[idx]

        # Handle Telegram tracks - download if needed
        if tr.source == "telegram" and not os.path.exists(tr.path):
            self._download_telegram_track(tr)
            return

        # Load and play once
        media = self.instance.media_new(tr.path)
        self.player.set_media(media)
        self.player.play()
        self.user_stopped = False
        self._set_playing_ui(True)

        # Apply volume/mute
        self.player.audio_set_volume(int(self.vol_var.get()))
        self.player.audio_set_mute(self.muted)

        # Update UI info
        self._update_now_playing_panel()

        # Update total time soon after playback starts
        self.after(300, self._update_total_time)

        # Reflect selection in listbox
        self._refresh_listbox()

    def _download_telegram_track(self, track: Track):
        """Download Telegram track for playback"""
        if not self.telegram_manager.connected:
            messagebox.showerror("Not Connected", "Not connected to Telegram.")
            return

        # Show download progress
        progress = tk.Toplevel(self)
        progress.title("Downloading Track")
        progress.geometry("300x100")
        progress.transient(self)
        progress.grab_set()

        ttk.Label(progress, text=f"Downloading: {track.title}").pack(pady=10)
        progress_bar = ttk.Progressbar(progress, mode='determinate')
        progress_bar.pack(fill="x", padx=20, pady=5)
        status_label = ttk.Label(progress, text="Starting download...")
        status_label.pack(pady=5)

        def progress_callback(current, total):
            if total > 0:
                percent = (current / total) * 100
                progress.after(0, lambda: progress_bar.config(value=percent))
                progress.after(0, lambda: status_label.config(
                    text=f"Downloading... {current}/{total} bytes ({percent:.1f}%)"
                ))

        def do_download():
            try:
                async def async_download():
                    return await self.telegram_manager.download_audio_file(
                        track,
                        progress_callback
                    )

                # Run async function in thread
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(async_download())
                loop.close()

                progress.after(0, lambda: on_download_complete(result))

            except Exception as e:
                progress.after(0, lambda: on_download_error(str(e)))

        def on_download_complete(file_path):
            progress.destroy()
            # Now play the track
            self._play_index(self.current_index)

        def on_download_error(error_msg):
            progress.destroy()
            messagebox.showerror("Download Error", f"Failed to download track:\n{error_msg}")

        # Start download in thread
        threading.Thread(target=do_download, daemon=True).start()

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
        # Manual stop should not trigger auto-advance
        self.user_stopped = True
        try:
            self.player.stop()
        except Exception:
            pass
        self._set_playing_ui(False)
        # Reset times
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
        self.repeat_mode = {"off": "one", "one": "all", "all": "off"}[self.repeat_mode]
        label = {"off": "Off", "one": "One", "all": "All"}[self.repeat_mode]
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
        # Only change position when user drags; avoids jitter during programmatic updates
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
                self.time_now.configure(text=self._fmt_time(max(0, now_ms / 1000.0)))
                self.time_total.configure(text=self._fmt_time(max(0, total_ms / 1000.0)))
            else:
                if not self.player.is_playing():
                    self.time_now.configure(text="0:00")
        except Exception:
            pass
        self.after(200, self._poll_position)

    # -------------------- VLC events --------------------
    def _on_vlc_end(self, event):
        # Called on a VLC thread -> schedule to Tk main thread
        self.after(0, self._on_media_end_mainthread)

    def _on_media_end_mainthread(self):
        if self.user_stopped:
            return  # honor manual stop
        # At end of track, advance according to repeat/shuffle
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
            self.lab_source.configure(text="Source: —")
            self.cover_label.configure(image="", text="")
            return

        tr = self.playlist[self.current_index]
        self.lab_title.configure(text=f"Title: {tr.title or os.path.basename(tr.path)}")
        self.lab_artist.configure(text=f"Artist: {tr.artist or '—'}")
        self.lab_album.configure(text=f"Album: {tr.album or '—'}")
        self.lab_file.configure(text=f"File: {tr.path}")
        self.lab_source.configure(text=f"Source: {tr.source.capitalize()}")

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
        # Try format-specific embedded art, then folder images
        if MutagenFile is not None and Image is not None:
            try:
                ext = os.path.splitext(path)[1].lower()
                if ext in (".mp3",):
                    mf = MutagenFile(path, easy=False)
                    if hasattr(mf, "tags") and mf.tags:
                        # ID3 APIC frames
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
                elif ext in (".m4a", ".mp4"):
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
                    # Generic attempt: some formats store APIC-like frames
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

        # Folder images fallback
        folder = os.path.dirname(path) or "."
        for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "AlbumArtSmall.jpg"):
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
            filetypes=[("M3U8 playlist", ".m3u8"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for t in self.playlist:
                    # Only save local files to playlist
                    if t.source == "local":
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
                    if os.path.isfile(line):
                        new_paths.append(line)
            if not new_paths:
                messagebox.showinfo("Empty", "No valid file paths found in playlist.")
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
            self.btn_repeat.configure(text=f"↻ Repeat: { {'off': 'Off', 'one': 'One', 'all': 'All'}[self.repeat_mode]}")
            paths = st.get("paths", [])
            keep = [p for p in paths if os.path.isfile(p)]
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
                "paths": [t.path for t in self.playlist if t.source == "local"],
                "current_index": self.current_index,
            }
            import json
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=2)
        except Exception:
            pass

        # Disconnect Telegram
        if self.telegram_manager.connected:
            self.telegram_manager.disconnect()

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
            "Python Music Player\n\nGUI: Tkinter\nPlayback: VLC\nTags: mutagen\nArt: Pillow\nTelegram: Telethon\n\n© 2025 Example"
        )


def main():
    app = MusicPlayerApp()
    app.protocol("WM_DELETE_WINDOW", app.on_quit)
    app.mainloop()


if __name__ == "__main__":
    main()