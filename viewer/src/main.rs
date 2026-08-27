//! 数値予報の GPU ビューア。
//!
//! **狙いは「境界を毎フレーム越えない」こと。** 今の Flet 経路は領域が
//! 変わるたびに Python が画像を作り直して渡すので、パン・ズームのたびに
//! 往復が起きる。ここでは格子を一度テクスチャに載せ、以後は表示範囲
//! (uniform の center/scale)を書き換えるだけで動かす。
//!
//! 借りているのは winit(窓・入力)と wgpu(描画)だけ。ウィジェット一式は
//! 持たない — 要るのが「格子を貼ってパン・ズーム」だけだから。

mod payload;

use std::path::PathBuf;
use std::sync::Arc;

use payload::Payload;
use winit::application::ApplicationHandler;
use winit::event::{ElementState, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, EventLoop};
use winit::window::{Window, WindowId};

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct View {
    center: [f32; 2],
    scale: [f32; 2],
    vmin: f32,
    vmax: f32,
    _pad: [f32; 2],       // 16 バイト境界に揃える
}

struct Gpu {
    surface: wgpu::Surface<'static>,
    device: wgpu::Device,
    queue: wgpu::Queue,
    config: wgpu::SurfaceConfiguration,
    pipeline: wgpu::RenderPipeline,
    bind: wgpu::BindGroup,
    ubo: wgpu::Buffer,
}

struct App {
    window: Option<Arc<Window>>,
    gpu: Option<Gpu>,
    payload: Payload,
    view: View,
    dragging: bool,
    last_cursor: (f64, f64),
    frames: u32,
    t0: std::time::Instant,
}

impl App {
    fn new(payload: Payload) -> Self {
        let (vmin, vmax) = (payload.vmin, payload.vmax);
        Self {
            window: None, gpu: None, payload,
            view: View { center: [0.5, 0.5], scale: [0.5, 0.5], vmin, vmax, _pad: [0.0; 2] },
            dragging: false, last_cursor: (0.0, 0.0),
            frames: 0, t0: std::time::Instant::now(),
        }
    }

    fn init_gpu(&mut self, window: Arc<Window>) {
        let size = window.inner_size();
        let instance = wgpu::Instance::default();
        let surface = instance.create_surface(window.clone()).expect("surface");
        let adapter = pollster::block_on(instance.request_adapter(
            &wgpu::RequestAdapterOptions {
                compatible_surface: Some(&surface), ..Default::default()
            })).expect("GPU が見つからない");
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor::default(), None)).expect("device");

        let caps = surface.get_capabilities(&adapter);
        let config = wgpu::SurfaceConfiguration {
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
            format: caps.formats[0],
            width: size.width.max(1), height: size.height.max(1),
            present_mode: wgpu::PresentMode::Fifo,
            alpha_mode: caps.alpha_modes[0],
            view_formats: vec![], desired_maximum_frame_latency: 2,
        };
        surface.configure(&device, &config);

        // 値の格子。R32Float で値のまま持つ(色にしない)
        let p = &self.payload;
        let tex = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("grid"),
            size: wgpu::Extent3d { width: p.nx as u32, height: p.ny as u32,
                                   depth_or_array_layers: 1 },
            mip_level_count: 1, sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::R32Float,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        queue.write_texture(
            tex.as_image_copy(), bytemuck::cast_slice(&p.data),
            wgpu::ImageDataLayout { offset: 0,
                bytes_per_row: Some(p.nx as u32 * 4), rows_per_image: Some(p.ny as u32) },
            wgpu::Extent3d { width: p.nx as u32, height: p.ny as u32,
                             depth_or_array_layers: 1 });

        // 配色は 256 段の LUT。Python 側 build_continuous_lut の出力そのまま
        let mut rgba = Vec::with_capacity(256 * 4);
        for i in 0..256 {
            rgba.extend_from_slice(&[p.lut[i * 3], p.lut[i * 3 + 1], p.lut[i * 3 + 2], 255]);
        }
        let lut_tex = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("lut"),
            size: wgpu::Extent3d { width: 256, height: 1, depth_or_array_layers: 1 },
            mip_level_count: 1, sample_count: 1,
            dimension: wgpu::TextureDimension::D1,
            format: wgpu::TextureFormat::Rgba8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        queue.write_texture(lut_tex.as_image_copy(), &rgba,
            wgpu::ImageDataLayout { offset: 0, bytes_per_row: Some(256 * 4), rows_per_image: None },
            wgpu::Extent3d { width: 256, height: 1, depth_or_array_layers: 1 });

        let ubo = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("view"), size: std::mem::size_of::<View>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let samp = device.create_sampler(&wgpu::SamplerDescriptor {
            mag_filter: wgpu::FilterMode::Linear,
            min_filter: wgpu::FilterMode::Linear, ..Default::default()
        });

        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("chart"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shader.wgsl").into()),
        });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: None,
            entries: &[
                wgpu::BindGroupLayoutEntry { binding: 0,
                    visibility: wgpu::ShaderStages::VERTEX_FRAGMENT,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false, min_binding_size: None },
                    count: None },
                wgpu::BindGroupLayoutEntry { binding: 1,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: true },
                        view_dimension: wgpu::TextureViewDimension::D2, multisampled: false },
                    count: None },
                wgpu::BindGroupLayoutEntry { binding: 2,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                    count: None },
                wgpu::BindGroupLayoutEntry { binding: 3,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: true },
                        view_dimension: wgpu::TextureViewDimension::D1, multisampled: false },
                    count: None },
                wgpu::BindGroupLayoutEntry { binding: 4,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                    count: None },
            ],
        });
        let bind = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: ubo.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: wgpu::BindingResource::TextureView(
                    &tex.create_view(&Default::default())) },
                wgpu::BindGroupEntry { binding: 2, resource: wgpu::BindingResource::Sampler(&samp) },
                wgpu::BindGroupEntry { binding: 3, resource: wgpu::BindingResource::TextureView(
                    &lut_tex.create_view(&Default::default())) },
                wgpu::BindGroupEntry { binding: 4, resource: wgpu::BindingResource::Sampler(&samp) },
            ],
        });
        let pl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: None, layout: Some(&pl),
            vertex: wgpu::VertexState { module: &shader, entry_point: "vs",
                buffers: &[], compilation_options: Default::default() },
            fragment: Some(wgpu::FragmentState { module: &shader, entry_point: "fs",
                targets: &[Some(config.format.into())],
                compilation_options: Default::default() }),
            primitive: Default::default(), depth_stencil: None,
            multisample: Default::default(), multiview: None, cache: None,
        });

        self.gpu = Some(Gpu { surface, device, queue, config, pipeline, bind, ubo });
    }

    fn draw(&mut self) {
        let Some(g) = &self.gpu else { return };
        g.queue.write_buffer(&g.ubo, 0, bytemuck::bytes_of(&self.view));
        let Ok(frame) = g.surface.get_current_texture() else { return };
        let view = frame.texture.create_view(&Default::default());
        let mut enc = g.device.create_command_encoder(&Default::default());
        {
            let mut rp = enc.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: None,
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &view, resolve_target: None,
                    ops: wgpu::Operations { load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                                            store: wgpu::StoreOp::Store },
                })],
                depth_stencil_attachment: None, timestamp_writes: None, occlusion_query_set: None,
            });
            rp.set_pipeline(&g.pipeline);
            rp.set_bind_group(0, &g.bind, &[]);
            rp.draw(0..6, 0..1);
        }
        g.queue.submit(Some(enc.finish()));
        frame.present();

        self.frames += 1;
        let dt = self.t0.elapsed().as_secs_f32();
        if dt >= 2.0 {
            println!("{:.0} fps ({} フレーム / {:.1} 秒)", self.frames as f32 / dt, self.frames, dt);
            self.frames = 0;
            self.t0 = std::time::Instant::now();
        }
    }
}

impl ApplicationHandler for App {
    fn resumed(&mut self, el: &ActiveEventLoop) {
        let attrs = Window::default_attributes()
            .with_title("AIseed Weather — GPU ビューア")
            .with_inner_size(winit::dpi::LogicalSize::new(1000, 700));
        let w = Arc::new(el.create_window(attrs).expect("窓"));
        // 検索欄でだけ日本語入力を使う想定。本文編集は無いので IME はこれで足りる
        w.set_ime_allowed(true);
        self.init_gpu(w.clone());
        self.window = Some(w);
    }

    fn window_event(&mut self, el: &ActiveEventLoop, _: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => el.exit(),
            WindowEvent::Resized(size) => {
                if let Some(g) = &mut self.gpu {
                    g.config.width = size.width.max(1);
                    g.config.height = size.height.max(1);
                    g.surface.configure(&g.device, &g.config);
                }
            }
            WindowEvent::MouseInput { state, button: MouseButton::Left, .. } => {
                self.dragging = state == ElementState::Pressed;
            }
            WindowEvent::CursorMoved { position, .. } => {
                if self.dragging {
                    // パン: 表示中心をずらすだけ。Python は呼ばない
                    if let Some(g) = &self.gpu {
                        let dx = (position.x - self.last_cursor.0) as f32 / g.config.width as f32;
                        let dy = (position.y - self.last_cursor.1) as f32 / g.config.height as f32;
                        self.view.center[0] -= dx * self.view.scale[0] * 2.0;
                        self.view.center[1] += dy * self.view.scale[1] * 2.0;
                    }
                    self.window.as_ref().map(|w| w.request_redraw());
                }
                self.last_cursor = (position.x, position.y);
            }
            WindowEvent::MouseWheel { delta, .. } => {
                // ズーム: 倍率を変えるだけ。再描画も再取得もしない
                let s = match delta {
                    MouseScrollDelta::LineDelta(_, y) => y,
                    MouseScrollDelta::PixelDelta(p) => p.y as f32 / 50.0,
                };
                let f = (1.0 - s * 0.1).clamp(0.5, 2.0);
                self.view.scale[0] = (self.view.scale[0] * f).clamp(0.005, 2.0);
                self.view.scale[1] = (self.view.scale[1] * f).clamp(0.005, 2.0);
                self.window.as_ref().map(|w| w.request_redraw());
            }
            WindowEvent::RedrawRequested => self.draw(),
            _ => {}
        }
    }
}

fn main() {
    let path = std::env::args().nth(1).map(PathBuf::from)
        .unwrap_or_else(|| { eprintln!("使い方: viewer <payload.bin>"); std::process::exit(2) });
    let payload = match Payload::load(&path) {
        Ok(p) => p,
        Err(e) => { eprintln!("読み込み失敗: {e}"); std::process::exit(1) }
    };
    println!("格子 {}x{} / 値域 {:.1}..{:.1}", payload.nx, payload.ny, payload.vmin, payload.vmax);
    println!("メタ {}", payload.meta);

    let el = EventLoop::new().expect("event loop");
    el.run_app(&mut App::new(payload)).expect("run");
}
