//! Bolo's Rust push-to-talk dictation runtime.

use std::collections::VecDeque;
use std::env;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use arboard::Clipboard;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{FromSample, Sample, SampleFormat, SizedSample, Stream, StreamConfig};
use rdev::{Event, EventType, Key};
use regex::Regex;
use reqwest::blocking::{Client, multipart};
use serde::{Deserialize, Serialize};
use tao::event::{Event as TaoEvent, StartCause};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use thiserror::Error;
use tracing::{error, info, warn};
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::EnvFilter;
use tray_icon::menu::{CheckMenuItem, Menu, MenuEvent, MenuId, MenuItem, Submenu};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

const LOG_FILE: &str = "/tmp/bolo.log";
const CHANNELS: u16 = 1;
const TELNYX_STT_ENDPOINT: &str = "https://api.telnyx.com/v2/ai/audio/transcriptions";
const TELNYX_LLM_ENDPOINT: &str = "https://api.telnyx.com/v2/ai/chat/completions";
const CORRECTION_WINDOW: Duration = Duration::from_secs(3);
const MIN_RECORDING: Duration = Duration::from_millis(500);

#[derive(Debug, Error)]
enum AppError {
    #[error("audio device is unavailable")]
    MissingAudioDevice,
    #[error("configuration value {0} is missing")]
    MissingConfig(&'static str),
    #[error("mutex was poisoned: {0}")]
    PoisonedMutex(String),
    #[error("I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("audio stream failed: {0}")]
    AudioStream(String),
    #[error("menu bar failed: {0}")]
    MenuBar(String),
    #[error("clipboard failed: {0}")]
    Clipboard(String),
    #[error("HTTP request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("JSON failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("event listener failed: {0:?}")]
    Listener(rdev::ListenError),
    #[error("transcription failed: {0}")]
    Transcription(String),
}

#[derive(Clone, Debug)]
struct Config {
    telnyx_api_key: String,
    llm_cleanup: CleanupMode,
    litellm_base: Option<String>,
    litellm_key: Option<String>,
    microphone: Option<String>,
    root_dir: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CleanupMode {
    Auto,
    On,
    Off,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DictationCommandKind {
    Scratch,
    Insert,
    Replace,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DictationCommand {
    kind: DictationCommandKind,
    text: String,
    display: String,
}

#[derive(Debug, Default)]
struct AppState {
    active: Option<ActiveRecording>,
    right_option_down: bool,
    last_result: Option<String>,
    correction_until: Option<Instant>,
    history: VecDeque<String>,
    selected_microphone: Option<String>,
}

struct ActiveRecording {
    stream: Stream,
    samples: Arc<Mutex<Vec<i16>>>,
    started_at: Instant,
    sample_rate: u32,
}

impl std::fmt::Debug for ActiveRecording {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ActiveRecording")
            .field("started_at", &self.started_at)
            .field("sample_rate", &self.sample_rate)
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
struct App {
    config: Config,
    http: Client,
    vocabulary: Vec<String>,
    state: Mutex<AppState>,
}

#[derive(Clone, Debug)]
enum UserEvent {
    Menu(MenuEvent),
}

struct TrayUi {
    tray_icon: TrayIcon,
    microphone_items: Vec<(String, CheckMenuItem)>,
    quit_item: MenuItem,
}

impl std::fmt::Debug for TrayUi {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TrayUi")
            .field("tray_id", self.tray_icon.id())
            .field("microphone_count", &self.microphone_items.len())
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Deserialize)]
struct SttResponse {
    text: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Debug, Deserialize)]
struct ChatChoice {
    message: ChatMessage,
}

#[derive(Debug, Deserialize)]
struct ChatMessage {
    content: String,
}

#[derive(Debug, Serialize)]
struct ChatRequest<'a> {
    model: &'a str,
    messages: Vec<ChatMessageRequest<'a>>,
    max_tokens: u16,
    temperature: u8,
    enable_thinking: bool,
}

#[derive(Debug, Serialize)]
struct ChatMessageRequest<'a> {
    role: &'a str,
    content: &'a str,
}

fn main() -> Result<(), AppError> {
    let _guard = setup_logging()?;
    let app = Arc::new(App::new()?);
    info!("Bolo Rust runtime started. Hold Right Option to dictate.");
    play_sound("Tink");
    run_app_event_loop(app)
}

fn setup_logging() -> Result<WorkerGuard, AppError> {
    let file = File::options().create(true).append(true).open(LOG_FILE)?;
    let (writer, guard) = tracing_appender::non_blocking(file);
    tracing_subscriber::fmt()
        .with_writer(writer)
        .with_env_filter(EnvFilter::new("info"))
        .with_ansi(false)
        .with_target(false)
        .try_init()
        .map_err(|error| AppError::AudioStream(error.to_string()))?;
    Ok(guard)
}

fn run_hotkey_listener(app: Arc<App>) -> Result<(), AppError> {
    rdev::listen(move |event| {
        if let Err(error) = app.handle_event(&event) {
            error!("{error}");
        }
    })
    .map_err(AppError::Listener)
}

fn run_app_event_loop(app: Arc<App>) -> Result<(), AppError> {
    let mut event_loop_builder = EventLoopBuilder::<UserEvent>::with_user_event();
    let event_loop = event_loop_builder.build();
    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some(move |event| {
        if proxy.send_event(UserEvent::Menu(event)).is_err() {
            warn!("menu event dropped because event loop is closed");
        }
    }));

    let listener_app = Arc::clone(&app);
    let listener_handle = std::thread::Builder::new()
        .name(String::from("bolo-hotkeys"))
        .spawn(move || {
            if let Err(error) = run_hotkey_listener(listener_app) {
                error!("{error}");
            }
        })?;
    drop(listener_handle);

    let mut tray_ui: Option<TrayUi> = None;
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            TaoEvent::NewEvents(StartCause::Init) => match create_tray_ui(&app) {
                Ok(ui) => {
                    tray_ui = Some(ui);
                    info!("menu bar icon ready");
                }
                Err(error) => error!("{error}"),
            },
            TaoEvent::UserEvent(UserEvent::Menu(menu_event)) => {
                if let Some(ui) = tray_ui.as_mut() {
                    handle_menu_event(&app, ui, &menu_event, control_flow);
                }
            }
            TaoEvent::NewEvents(_)
            | TaoEvent::WindowEvent { .. }
            | TaoEvent::DeviceEvent { .. }
            | TaoEvent::Suspended
            | TaoEvent::Resumed
            | TaoEvent::MainEventsCleared
            | TaoEvent::RedrawRequested(_)
            | TaoEvent::RedrawEventsCleared
            | TaoEvent::LoopDestroyed
            | _ => {}
        }
    });
}

impl App {
    fn new() -> Result<Self, AppError> {
        let root_dir = app_root_dir()?;
        let config = Config::load(root_dir)?;
        let selected_microphone = config.microphone.clone();
        let vocabulary = load_vocabulary(&config.root_dir);
        let http = Client::builder().timeout(Duration::from_secs(12)).build()?;
        Ok(Self {
            config,
            http,
            vocabulary,
            state: Mutex::new(AppState {
                selected_microphone,
                ..AppState::default()
            }),
        })
    }

    fn handle_event(self: &Arc<Self>, event: &Event) -> Result<(), AppError> {
        match event.event_type {
            EventType::KeyPress(key) if is_right_option(key) => self.handle_press(),
            EventType::KeyRelease(key) if is_right_option(key) => self.handle_release(),
            EventType::KeyPress(_)
            | EventType::KeyRelease(_)
            | EventType::ButtonPress(_)
            | EventType::ButtonRelease(_)
            | EventType::MouseMove { .. }
            | EventType::Wheel { .. } => Ok(()),
        }
    }

    fn handle_press(&self) -> Result<(), AppError> {
        {
            let mut state = self.lock_state()?;
            if state.right_option_down || state.active.is_some() {
                return Ok(());
            }
            state.right_option_down = true;
        }
        let selected_microphone = {
            let state = self.lock_state()?;
            state.selected_microphone.clone()
        };
        let recording = match start_recording(selected_microphone.as_deref()) {
            Ok(recording) => recording,
            Err(error) => {
                let mut state = self.lock_state()?;
                state.right_option_down = false;
                drop(state);
                return Err(error);
            }
        };
        {
            let mut state = self.lock_state()?;
            state.active = Some(recording);
        }
        info!("recording started");
        play_sound("Tink");
        Ok(())
    }

    fn handle_release(self: &Arc<Self>) -> Result<(), AppError> {
        let recording = {
            let mut state = self.lock_state()?;
            state.right_option_down = false;
            state.active.take()
        };
        if let Some(recording) = recording {
            let app = Arc::clone(self);
            let _join_handle = std::thread::Builder::new()
                .name(String::from("bolo-pipeline"))
                .spawn(move || {
                    if let Err(error) = app.finish_recording(recording) {
                        error!("{error}");
                        play_sound("Basso");
                    }
                })?;
        }
        Ok(())
    }

    fn finish_recording(&self, recording: ActiveRecording) -> Result<(), AppError> {
        let elapsed = recording.started_at.elapsed();
        drop(recording.stream);
        if elapsed < MIN_RECORDING {
            info!("recording ignored because it was too short");
            return Ok(());
        }
        let samples = recording
            .samples
            .lock()
            .map_err(|error| AppError::PoisonedMutex(error.to_string()))?
            .clone();
        if samples.is_empty() {
            info!("recording contained no samples");
            return Ok(());
        }
        info!(
            "recording stopped; samples={}, sample_rate={}",
            samples.len(),
            recording.sample_rate
        );
        let wav = wav_bytes(&samples, recording.sample_rate)?;
        let raw = self.transcribe(&wav)?;
        let text = self.prepare_text(&raw)?;
        if text.is_empty() {
            return Ok(());
        }
        let command = {
            let state = self.lock_state()?;
            parse_command(&text, correction_active(&state))
        };
        if let Some(command) = command {
            self.apply_command(command)?;
        } else {
            paste_text(&text)?;
            self.remember_result(text)?;
            play_sound("Pop");
        }
        Ok(())
    }

    fn transcribe(&self, wav: &[u8]) -> Result<String, AppError> {
        info!("sending batch transcription request");
        let model_config = serde_json::json!({
            "smart_format": true,
            "punctuate": true,
            "keyterms": self.vocabulary.iter().take(50).collect::<Vec<_>>()
        });
        let prompt = build_stt_prompt(&self.vocabulary);
        let part = multipart::Part::bytes(wav.to_vec())
            .file_name(String::from("audio.wav"))
            .mime_str("audio/wav")?;
        let mut form = multipart::Form::new()
            .text("model", String::from("deepgram/nova-3"))
            .text("language", String::from("en"))
            .text("model_config", serde_json::to_string(&model_config)?)
            .part("file", part);
        if let Some(prompt) = prompt {
            form = form.text("prompt", prompt);
        }
        let response = self
            .http
            .post(TELNYX_STT_ENDPOINT)
            .bearer_auth(&self.config.telnyx_api_key)
            .multipart(form)
            .send()?;
        let status = response.status();
        if !status.is_success() {
            return Err(AppError::Transcription(format!(
                "Telnyx STT returned {status}"
            )));
        }
        let parsed: SttResponse = response.json()?;
        Ok(parsed.text.unwrap_or_default())
    }

    fn prepare_text(&self, raw: &str) -> Result<String, AppError> {
        let normalized = canonicalize_known_terms(&normalize_transcript(raw));
        let stripped = remove_fillers(&normalized)?;
        if !should_run_cleanup(&self.config, &stripped) {
            return Ok(stripped);
        }
        match self.cleanup_transcript(&stripped) {
            Ok(cleaned) if !cleaned.is_empty() => Ok(canonicalize_known_terms(
                &normalize_transcript(cleaned.trim()),
            )),
            Ok(_) => Ok(stripped),
            Err(error) => {
                warn!("cleanup skipped after error: {error}");
                Ok(stripped)
            }
        }
    }

    fn cleanup_transcript(&self, transcript: &str) -> Result<String, AppError> {
        let endpoint = self.config.llm_endpoint();
        let model = self.config.llm_model();
        let request = ChatRequest {
            model: &model,
            messages: vec![
                ChatMessageRequest {
                    role: "system",
                    content: cleanup_prompt(),
                },
                ChatMessageRequest {
                    role: "user",
                    content: transcript,
                },
            ],
            max_tokens: 1_500,
            temperature: 0,
            enable_thinking: false,
        };
        let mut builder = self.http.post(endpoint).json(&request);
        if let Some(key) = self.config.llm_key() {
            builder = builder.bearer_auth(key);
        }
        let response = builder.send()?;
        if !response.status().is_success() {
            return Ok(String::new());
        }
        let parsed: ChatResponse = response.json()?;
        Ok(parsed
            .choices
            .first()
            .map_or_else(String::new, |choice| choice.message.content.clone()))
    }

    fn apply_command(&self, command: DictationCommand) -> Result<(), AppError> {
        match command.kind {
            DictationCommandKind::Scratch => {
                let last = {
                    let mut state = self.lock_state()?;
                    state.last_result.take()
                };
                if let Some(text) = last {
                    delete_chars(text.chars().count())?;
                }
                info!("applied scratch command");
            }
            DictationCommandKind::Insert => {
                paste_text(&command.text)?;
                self.remember_result(command.text)?;
                play_sound("Pop");
            }
            DictationCommandKind::Replace => {
                let previous = {
                    let state = self.lock_state()?;
                    state.last_result.clone()
                };
                if let Some(text) = previous {
                    delete_chars(text.chars().count())?;
                }
                paste_text(&command.text)?;
                self.remember_result(command.text)?;
                play_sound("Pop");
            }
        }
        Ok(())
    }

    fn remember_result(&self, text: String) -> Result<(), AppError> {
        let mut state = self.lock_state()?;
        state.last_result = Some(text.clone());
        state.correction_until = Some(Instant::now() + CORRECTION_WINDOW);
        state.history.push_front(text);
        state.history.truncate(10);
        drop(state);
        Ok(())
    }

    fn lock_state(&self) -> Result<MutexGuard<'_, AppState>, AppError> {
        self.state
            .lock()
            .map_err(|error| AppError::PoisonedMutex(error.to_string()))
    }

    fn selected_microphone(&self) -> Result<Option<String>, AppError> {
        let state = self.lock_state()?;
        Ok(state.selected_microphone.clone())
    }

    fn set_microphone(&self, microphone: &str) -> Result<(), AppError> {
        let mut state = self.lock_state()?;
        state.selected_microphone = Some(microphone.to_owned());
        drop(state);
        info!("selected microphone: {microphone}");
        Ok(())
    }
}

impl Config {
    fn load(root_dir: PathBuf) -> Result<Self, AppError> {
        let telnyx_api_key =
            load_env_value("TELNYX_API_KEY").ok_or(AppError::MissingConfig("TELNYX_API_KEY"))?;
        let llm_cleanup = match load_env_value("BOLO_LLM_CLEANUP")
            .unwrap_or_else(|| String::from("auto"))
            .to_ascii_lowercase()
            .as_str()
        {
            "off" => CleanupMode::Off,
            "on" => CleanupMode::On,
            _ => CleanupMode::Auto,
        };
        Ok(Self {
            telnyx_api_key,
            llm_cleanup,
            litellm_base: load_env_value("LITELLM_BASE"),
            litellm_key: load_env_value("LITELLM_KEY"),
            microphone: load_env_value("BOLO_MICROPHONE"),
            root_dir,
        })
    }

    fn llm_endpoint(&self) -> String {
        self.litellm_base.as_ref().map_or_else(
            || String::from(TELNYX_LLM_ENDPOINT),
            |base| {
                let trimmed = base.trim_end_matches('/');
                if trimmed.ends_with("/v1") {
                    format!("{trimmed}/chat/completions")
                } else {
                    format!("{trimmed}/v1/chat/completions")
                }
            },
        )
    }

    fn llm_key(&self) -> Option<&str> {
        self.litellm_key
            .as_deref()
            .filter(|key| !key.is_empty())
            .or(Some(self.telnyx_api_key.as_str()))
    }

    fn llm_model(&self) -> String {
        if self.litellm_base.is_some() {
            String::from("MiniMax-M2.5-drop")
        } else {
            String::from("Qwen/Qwen3-235B-A22B")
        }
    }
}

fn start_recording(selected_microphone: Option<&str>) -> Result<ActiveRecording, AppError> {
    let host = cpal::default_host();
    let device = select_input_device(&host, selected_microphone)?;
    let device_name = device_name(&device);
    let supported = device
        .default_input_config()
        .map_err(|error| AppError::AudioStream(error.to_string()))?;
    let config = StreamConfig::from(supported.clone());
    let samples = Arc::new(Mutex::new(Vec::<i16>::new()));
    let channels = usize::from(config.channels);
    info!(
        "recording with input device={device_name:?}, sample_rate={}, channels={}, format={:?}",
        config.sample_rate,
        config.channels,
        supported.sample_format()
    );
    let stream = match supported.sample_format() {
        SampleFormat::I16 => build_stream::<i16>(&device, &config, channels, Arc::clone(&samples))?,
        SampleFormat::F32 => build_stream::<f32>(&device, &config, channels, Arc::clone(&samples))?,
        SampleFormat::U16 => build_stream::<u16>(&device, &config, channels, Arc::clone(&samples))?,
        other @ (SampleFormat::I8
        | SampleFormat::I24
        | SampleFormat::I32
        | SampleFormat::I64
        | SampleFormat::U8
        | SampleFormat::U24
        | SampleFormat::U32
        | SampleFormat::U64
        | SampleFormat::F64
        | SampleFormat::DsdU8
        | SampleFormat::DsdU16
        | SampleFormat::DsdU32
        | _) => {
            return Err(AppError::AudioStream(format!(
                "unsupported format {other:?}"
            )));
        }
    };
    stream
        .play()
        .map_err(|error| AppError::AudioStream(error.to_string()))?;
    Ok(ActiveRecording {
        stream,
        samples,
        started_at: Instant::now(),
        sample_rate: config.sample_rate,
    })
}

fn select_input_device(
    host: &cpal::Host,
    selected_microphone: Option<&str>,
) -> Result<cpal::Device, AppError> {
    let devices = host
        .input_devices()
        .map_err(|error| AppError::AudioStream(error.to_string()))?;
    let mut fallback = None;
    let mut names = Vec::new();
    for device in devices {
        let name = device_name(&device);
        names.push(name.clone());
        if fallback.is_none() {
            fallback = Some(device.clone());
        }
        if let Some(selected) = selected_microphone
            && (name == selected
                || name
                    .to_ascii_lowercase()
                    .contains(&selected.to_ascii_lowercase()))
        {
            info!("available microphones: {}", names.join(", "));
            return Ok(device);
        }
    }
    info!("available microphones: {}", names.join(", "));
    if selected_microphone.is_some() {
        warn!("selected microphone not found; using system default");
    }
    host.default_input_device()
        .or(fallback)
        .ok_or(AppError::MissingAudioDevice)
}

fn input_device_names() -> Result<Vec<String>, AppError> {
    let host = cpal::default_host();
    let devices = host
        .input_devices()
        .map_err(|error| AppError::AudioStream(error.to_string()))?;
    let mut names = Vec::new();
    for device in devices {
        let name = device_name(&device);
        if !names.contains(&name) {
            names.push(name);
        }
    }
    Ok(names)
}

fn device_name(device: &cpal::Device) -> String {
    device.description().map_or_else(
        |_| String::from("unknown input device"),
        |description| description.name().to_owned(),
    )
}

fn create_tray_ui(app: &App) -> Result<TrayUi, AppError> {
    let tray_menu = Menu::new();
    let title = MenuItem::new("Bolo - Hold Right Option", false, None);
    tray_menu
        .append(&title)
        .map_err(|error| AppError::MenuBar(error.to_string()))?;

    let microphone_menu = Submenu::new("Microphone", true);
    let selected_microphone = app.selected_microphone()?;
    let devices = input_device_names()?;
    let mut microphone_items = Vec::new();
    if devices.is_empty() {
        let empty_item = MenuItem::new("No input devices found", false, None);
        microphone_menu
            .append(&empty_item)
            .map_err(|error| AppError::MenuBar(error.to_string()))?;
    } else {
        for (index, name) in devices.iter().enumerate() {
            let checked = selected_microphone
                .as_ref()
                .is_some_and(|selected| selected == name)
                || (selected_microphone.is_none() && index == 0);
            let item = CheckMenuItem::with_id(
                MenuId::new(format!("microphone:{index}")),
                name,
                true,
                checked,
                None,
            );
            microphone_menu
                .append(&item)
                .map_err(|error| AppError::MenuBar(error.to_string()))?;
            microphone_items.push((name.clone(), item));
        }
    }
    tray_menu
        .append(&microphone_menu)
        .map_err(|error| AppError::MenuBar(error.to_string()))?;

    let quit_item = MenuItem::with_id("quit", "Quit Bolo", true, None);
    tray_menu
        .append(&quit_item)
        .map_err(|error| AppError::MenuBar(error.to_string()))?;

    let tray_icon = TrayIconBuilder::new()
        .with_menu(Box::new(tray_menu))
        .with_tooltip("Bolo")
        .with_icon(tray_icon_image()?)
        .with_icon_as_template(true)
        .build()
        .map_err(|error| AppError::MenuBar(error.to_string()))?;

    Ok(TrayUi {
        tray_icon,
        microphone_items,
        quit_item,
    })
}

fn handle_menu_event(
    app: &App,
    tray_ui: &TrayUi,
    event: &MenuEvent,
    control_flow: &mut ControlFlow,
) {
    let event_id = event.id().as_ref();
    if event_id == tray_ui.quit_item.id().as_ref() {
        *control_flow = ControlFlow::Exit;
        return;
    }
    if let Some((selected_name, _)) = tray_ui
        .microphone_items
        .iter()
        .find(|(_, item)| event_id == item.id().as_ref())
    {
        if let Err(error) = app.set_microphone(selected_name) {
            error!("{error}");
            return;
        }
        for (name, item) in &tray_ui.microphone_items {
            item.set_checked(name == selected_name);
        }
    }
}

fn tray_icon_image() -> Result<Icon, AppError> {
    let width = 16_u32;
    let height = 16_u32;
    let pixel_count = usize::try_from(width.saturating_mul(height).saturating_mul(4))
        .map_err(|error| AppError::MenuBar(error.to_string()))?;
    let mut rgba = Vec::with_capacity(pixel_count);
    for y in 0..height {
        for x in 0..width {
            let in_mark = (4..=11).contains(&x) && (3..=12).contains(&y);
            if in_mark {
                rgba.extend_from_slice(&[0, 0, 0, 255]);
            } else {
                rgba.extend_from_slice(&[0, 0, 0, 0]);
            }
        }
    }
    Icon::from_rgba(rgba, width, height).map_err(|error| AppError::MenuBar(error.to_string()))
}

fn build_stream<T>(
    device: &cpal::Device,
    config: &StreamConfig,
    channels: usize,
    samples: Arc<Mutex<Vec<i16>>>,
) -> Result<Stream, AppError>
where
    T: Sample + SizedSample,
    i16: FromSample<T>,
{
    device
        .build_input_stream(
            config,
            move |data: &[T], _info: &cpal::InputCallbackInfo| {
                if let Ok(mut guard) = samples.try_lock() {
                    guard.extend(data.iter().step_by(channels).copied().map(i16::from_sample));
                }
            },
            |error| {
                warn!("audio callback failed: {error}");
            },
            None,
        )
        .map_err(|error| AppError::AudioStream(error.to_string()))
}

fn wav_bytes(samples: &[i16], sample_rate: u32) -> Result<Vec<u8>, AppError> {
    let data_len = u32::try_from(
        samples
            .len()
            .checked_mul(2)
            .ok_or_else(|| AppError::AudioStream(String::from("recording is too large")))?,
    )
    .map_err(|error| AppError::AudioStream(error.to_string()))?;
    let mut bytes = Vec::with_capacity(44_usize.saturating_add(samples.len().saturating_mul(2)));
    bytes.extend_from_slice(b"RIFF");
    bytes.extend_from_slice(&(36_u32.saturating_add(data_len)).to_le_bytes());
    bytes.extend_from_slice(b"WAVEfmt ");
    bytes.extend_from_slice(&16_u32.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&CHANNELS.to_le_bytes());
    bytes.extend_from_slice(&sample_rate.to_le_bytes());
    let byte_rate = sample_rate
        .checked_mul(u32::from(CHANNELS))
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| AppError::AudioStream(String::from("invalid sample rate")))?;
    bytes.extend_from_slice(&byte_rate.to_le_bytes());
    bytes.extend_from_slice(&(CHANNELS.saturating_mul(2)).to_le_bytes());
    bytes.extend_from_slice(&16_u16.to_le_bytes());
    bytes.extend_from_slice(b"data");
    bytes.extend_from_slice(&data_len.to_le_bytes());
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    Ok(bytes)
}

const fn is_right_option(key: Key) -> bool {
    matches!(key, Key::AltGr | Key::Alt)
}

fn parse_command(text: &str, correction_active: bool) -> Option<DictationCommand> {
    let stripped = text.trim();
    let lowered = stripped.to_ascii_lowercase();
    let command = match lowered.as_str() {
        "scratch that" => DictationCommand {
            kind: DictationCommandKind::Scratch,
            text: String::new(),
            display: String::new(),
        },
        "new paragraph" => insert_command("\n\n", "\\n\\n"),
        "new line" => insert_command("\n", "\\n"),
        "bullet" => insert_command("\n- ", "- "),
        "comma" => insert_command(", ", ","),
        "period" | "full stop" => insert_command(". ", "."),
        "question mark" => insert_command("? ", "?"),
        "exclamation mark" | "exclamation point" => insert_command("! ", "!"),
        "open quote" | "close quote" => insert_command("\"", "\""),
        "dash" => insert_command(" -- ", "--"),
        "colon" => insert_command(": ", ":"),
        "semicolon" => insert_command("; ", ";"),
        _ if lowered.starts_with("bullet ") => {
            let bullet_text = stripped.trim_start_matches("bullet ").trim();
            insert_command(&format!("\n- {bullet_text}"), &format!("- {bullet_text}"))
        }
        _ if lowered.starts_with("actually ") && correction_active => DictationCommand {
            kind: DictationCommandKind::Replace,
            text: stripped.trim_start_matches("actually ").trim().to_owned(),
            display: stripped.trim_start_matches("actually ").trim().to_owned(),
        },
        _ => return None,
    };
    Some(command)
}

fn insert_command(text: &str, display: &str) -> DictationCommand {
    DictationCommand {
        kind: DictationCommandKind::Insert,
        text: text.to_owned(),
        display: display.to_owned(),
    }
}

fn correction_active(state: &AppState) -> bool {
    state
        .correction_until
        .is_some_and(|deadline| Instant::now() < deadline)
}

fn normalize_transcript(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn canonicalize_known_terms(text: &str) -> String {
    let replacements = [
        (
            r"(?i)\bwhisper flow\b|\bwisper flow\b|\bvoisei\b|\bvoisey\b",
            "Wispr Flow",
        ),
        (r"(?i)\btelnyx\b|\btelenix\b|\btennix\b", "Telnyx"),
        (r"(?i)\bbolo\b|\bbollo\b", "Bolo"),
        (r"(?i)\bremotion\b|\bemotion\b|\bemotions\b", "Remotion"),
        (r"(?i)\bnova[ -]three\b|\bnova 3\b", "nova-3"),
        (r"(?i)\bquen\b|\bqueue when\b|\bkyuen\b|\bkwan\b", "Qwen"),
    ];
    let mut result = text.to_owned();
    for (pattern, replacement) in replacements {
        if let Ok(regex) = Regex::new(pattern) {
            result = regex.replace_all(&result, replacement).into_owned();
        }
    }
    result.trim().to_owned()
}

fn remove_fillers(text: &str) -> Result<String, AppError> {
    let patterns = [
        (r"(?i)\b(um+|uh+|hmm+|mhm)\b[,.]?\s*", ""),
        (r"(?i)\byou know[,.]?\s*", ""),
        (r"(?i)\band all[,.]?\s*$", ""),
        (r"(?i),?\s*\bright\??\s*$", ""),
        (r" {2,}", " "),
        (r"^\s*[,;]\s*", ""),
        (r"\s+([.,!?;:])", "$1"),
    ];
    let mut result = text.trim().to_owned();
    for (pattern, replacement) in patterns {
        let regex =
            Regex::new(pattern).map_err(|error| AppError::Transcription(error.to_string()))?;
        result = regex.replace_all(&result, replacement).into_owned();
    }
    Ok(result.trim().to_owned())
}

fn should_run_cleanup(config: &Config, transcript: &str) -> bool {
    match config.llm_cleanup {
        CleanupMode::Off => false,
        CleanupMode::On => true,
        CleanupMode::Auto => {
            transcript.split_whitespace().count() >= 6 && !looks_codeish(transcript)
        }
    }
}

fn looks_codeish(transcript: &str) -> bool {
    transcript.contains([
        '`', '{', '}', '(', ')', '[', ']', '<', '>', '_', '=', '\\', '/',
    ]) || [
        "function ",
        "const ",
        "let ",
        "class ",
        "import ",
        "return ",
        "camel case",
        "snake case",
    ]
    .iter()
    .any(|token| transcript.to_ascii_lowercase().contains(token))
}

fn build_stt_prompt(vocabulary: &[String]) -> Option<String> {
    if vocabulary.is_empty() {
        return None;
    }
    let prompt = vocabulary
        .iter()
        .take(50)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(", ");
    Some(prompt.chars().take(896).collect())
}

const fn cleanup_prompt() -> &'static str {
    "Apply minimal capitalization and punctuation fixes to a raw speech transcript. Do not rewrite meaning, summarize, add facts, or remove meaningful content. Remove only clear filler words. Output only the cleaned transcript."
}

fn load_vocabulary(root_dir: &Path) -> Vec<String> {
    let mut terms = Vec::new();
    let mut seen = Vec::<String>::new();
    for path in [
        root_dir.join("vocabulary.json"),
        home_path(".bolo_vocabulary.json"),
    ] {
        if let Some(loaded) = read_vocabulary_file(&path) {
            for term in loaded {
                let key = term.to_ascii_lowercase();
                if !seen.contains(&key) {
                    seen.push(key);
                    terms.push(term);
                }
            }
        }
    }
    terms
}

fn read_vocabulary_file(path: &Path) -> Option<Vec<String>> {
    let text = fs::read_to_string(path).ok()?;
    let values = serde_json::from_str::<Vec<String>>(&text).ok()?;
    Some(
        values
            .into_iter()
            .map(|term| term.trim().to_owned())
            .filter(|term| !term.is_empty())
            .collect(),
    )
}

fn load_env_value(name: &'static str) -> Option<String> {
    if let Ok(value) = env::var(name) {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_owned());
        }
    }
    let env_file = home_path(".bolo/env");
    if let Some(value) = read_key_value_file(&env_file, name) {
        return Some(value);
    }
    read_shell_export(&home_path(".zshrc"), name)
}

fn read_key_value_file(path: &Path, name: &str) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some((key, value)) = trimmed.split_once('=') else {
            continue;
        };
        if key.trim() == name {
            let cleaned = value.trim().trim_matches(['"', '\'']);
            if !cleaned.is_empty() {
                return Some(cleaned.to_owned());
            }
        }
    }
    None
}

fn read_shell_export(path: &Path, name: &str) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    let prefix = format!("export {name}=");
    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(value) = trimmed.strip_prefix(&prefix) {
            let cleaned = value.trim().trim_matches(['"', '\'']);
            if !cleaned.is_empty() {
                return Some(cleaned.to_owned());
            }
        }
    }
    None
}

fn paste_text(text: &str) -> Result<(), AppError> {
    let mut clipboard = Clipboard::new().map_err(|error| AppError::Clipboard(error.to_string()))?;
    let previous = clipboard.get_text().ok();
    clipboard
        .set_text(text.to_owned())
        .map_err(|error| AppError::Clipboard(error.to_string()))?;
    run_osascript("tell application \"System Events\" to keystroke \"v\" using command down")?;
    std::thread::sleep(Duration::from_millis(50));
    if let Some(previous) = previous {
        clipboard
            .set_text(previous)
            .map_err(|error| AppError::Clipboard(error.to_string()))?;
    }
    info!("pasted {} chars", text.chars().count());
    Ok(())
}

fn delete_chars(count: usize) -> Result<(), AppError> {
    for _ in 0..count {
        run_osascript("tell application \"System Events\" to key code 51")?;
    }
    Ok(())
}

fn run_osascript(script: &str) -> Result<(), AppError> {
    let status = Command::new("osascript").arg("-e").arg(script).status()?;
    if status.success() {
        Ok(())
    } else {
        Err(AppError::Io(std::io::Error::other("osascript failed")))
    }
}

fn play_sound(name: &str) {
    let path = format!("/System/Library/Sounds/{name}.aiff");
    match Command::new("afplay").arg(path).spawn() {
        Ok(mut child) => match child.try_wait() {
            Ok(Some(status)) if !status.success() => warn!("sound exited with {status}"),
            Ok(Some(_) | None) => {}
            Err(error) => warn!("sound status failed: {error}"),
        },
        Err(error) => warn!("sound failed: {error}"),
    }
}

fn home_path(relative: &str) -> PathBuf {
    env::var_os("HOME").map_or_else(
        || PathBuf::from(relative),
        |home| PathBuf::from(home).join(relative),
    )
}

fn app_root_dir() -> Result<PathBuf, AppError> {
    let current = env::current_dir()?;
    if current.join("vocabulary.json").exists() {
        return Ok(current);
    }
    let executable = env::current_exe()?;
    for ancestor in executable.ancestors() {
        if ancestor.join("vocabulary.json").exists() {
            return Ok(ancestor.to_path_buf());
        }
    }
    Ok(executable.parent().map_or(current, Path::to_path_buf))
}

#[cfg(test)]
mod tests {
    use super::{
        DictationCommandKind, build_stt_prompt, canonicalize_known_terms, parse_command,
        remove_fillers, wav_bytes,
    };

    #[test]
    fn parses_dictation_commands() {
        assert_eq!(
            parse_command("scratch that", false).map(|command| command.kind),
            Some(DictationCommandKind::Scratch)
        );

        let bullet = parse_command("bullet ship the Rust port", false);
        assert_eq!(
            bullet.as_ref().map(|command| command.kind),
            Some(DictationCommandKind::Insert)
        );
        assert_eq!(
            bullet.as_ref().map(|command| command.text.as_str()),
            Some("\n- ship the Rust port")
        );

        assert!(parse_command("actually corrected text", false).is_none());
        let replace = parse_command("actually corrected text", true);
        assert_eq!(
            replace.as_ref().map(|command| command.kind),
            Some(DictationCommandKind::Replace)
        );
        assert_eq!(
            replace.as_ref().map(|command| command.text.as_str()),
            Some("corrected text")
        );
    }

    #[test]
    fn normalizes_common_transcription_artifacts() {
        let canonical = canonicalize_known_terms("telnyx uses nova three for bolo");
        assert_eq!(canonical, "Telnyx uses nova-3 for Bolo");

        assert_eq!(
            remove_fillers("um, you know, ship it, right?")
                .ok()
                .as_deref(),
            Some("ship it")
        );
    }

    #[test]
    fn builds_bounded_stt_prompt() {
        let vocabulary = vec![String::from("Telnyx"), String::from("Wispr Flow")];
        assert_eq!(
            build_stt_prompt(&vocabulary),
            Some(String::from("Telnyx, Wispr Flow"))
        );

        let long = vec!["x".repeat(1_000)];
        assert_eq!(
            build_stt_prompt(&long).map(|prompt| prompt.len()),
            Some(896)
        );
    }

    #[test]
    fn encodes_pcm_as_wav() {
        let wav = wav_bytes(&[0, 1, -1], 16_000).unwrap_or_default();
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[36..40], b"data");
        assert_eq!(wav.len(), 50);
    }
}
