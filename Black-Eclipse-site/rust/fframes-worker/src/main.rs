use std::process::ExitCode;

use fframes::cli::{self, clap};
use fframes::{
    animation::Easing, AudioMap, Color, Duration, FFramesContext, Frame, RenderOptions, Svgr,
    Transform, Video,
};

mod fonts;
mod scenes;
mod spec;
mod theme;

struct TitleCard;

impl Video for TitleCard {
    const FPS: usize = 30;
    const WIDTH: usize = 1920;
    const HEIGHT: usize = 1080;
    const BACKGROUND_COLOR: Color = Color::BLACK;

    fn duration(&self) -> Duration<'_> {
        Duration::Seconds(10.0)
    }

    fn audio(&self) -> AudioMap<'_> {
        AudioMap::none()
    }

    fn render_frame(&self, frame: Frame, ctx: &FFramesContext) -> Svgr<'_> {
        fframes::svgr!(
            <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 1920 1080"
                width={ctx.current_video_size.width}
                height={ctx.current_video_size.height}
            >
                <defs>
                    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
                        <stop offset="0" stop-color="#0a0a12" />
                        <stop offset="1" stop-color="#141126" />
                    </linearGradient>
                </defs>

                <rect width="1920" height="1080" fill="url(#bg)" />

                <g transform={frame.animate(fframes::timeline!(
                    at 0.2 => 1.0, animate Transform::translate(-320, 0) => Transform::translate(0, 0), Easing::CubicBezier(0.22, 1.0, 0.36, 1.0),
                ))}>
                    <rect x="160" y="548" width="128" height="8" rx="4" fill="#3b82f6" />
                </g>

                <g
                    opacity={frame.animate(fframes::timeline!(
                        at 0.4 => 1.2, animate 0.0 => 1.0, Easing::EaseOut,
                    ))}
                    transform={frame.animate(fframes::timeline!(
                        at 0.4 => 1.2, animate Transform::translate(0, 40) => Transform::translate(0, 0), Easing::EaseOut,
                    ))}
                >
                    <text x="160" y="500" font-family={fonts::SANS} font-size="118" font-weight="600" fill="#f8fafc">
                        "BLACK ECLIPSE"
                    </text>
                </g>

                <g
                    opacity={frame.animate(fframes::timeline!(
                        at 1.0 => 1.8, animate 0.0 => 1.0, Easing::EaseOut,
                    ))}
                    transform={frame.animate(fframes::timeline!(
                        at 1.0 => 1.8, animate Transform::translate(0, 24) => Transform::translate(0, 0), Easing::EaseOut,
                    ))}
                >
                    <text x="164" y="616" font-family={fonts::SANS} font-size="40" font-weight="300" fill="#94a3b8">
                        "Video pipeline - Phase 1 - Windows proof of concept"
                    </text>
                </g>

                <g opacity={frame.animate(fframes::timeline!(
                    at 1.2 => 2.0, animate 0.0 => 1.0, Easing::EaseOut,
                ))}>
                    <text x="164" y="684" font-family={fonts::MONO} font-size="28" fill="#64748b">
                        "fframes 1.2.0 - cpu backend - 1920x1080@30"
                    </text>
                </g>

                <text x="1760" y="1020" text-anchor="end" font-family={fonts::MONO} font-size="26" fill="#334155">
                    {format!("t={:.1}s  f={}", frame.seconds(), frame.index)}
                </text>

                <rect
                    x="0" y="1074" height="6"
                    width={frame.animate(fframes::timeline!(
                        at 0. => 10., animate 1.0 => 1920.0, Easing::Linear,
                    ))}
                    fill="#3b82f6"
                />

                <rect width="1920" height="1080" fill="#000000" opacity={frame.animate(fframes::timeline!(
                    at 8.5 => 10.0, animate 0.0 => 1.0, Easing::EaseIn,
                ))} />
            </svg>
        )
    }
}

#[derive(Debug, clap::Args)]
struct WorkerArgs {
    /// Render a storyboard project spec (JSON, schemaVersion 2) instead of the built-in title card.
    #[arg(long, global = true)]
    project: Option<std::path::PathBuf>,
}

fn main() -> ExitCode {
    let args = cli::parse::<WorkerArgs>();
    // The bundled faces are linked into the process and registered with the
    // renderer, so the layout engine measures exactly what it draws — on any OS.
    let fonts = fonts::media_provider();
    let options = RenderOptions {
        load_system_fonts: true,
        default_font: fonts::SANS,
        media: Some(&fonts),
        ..Default::default()
    };
    match args.app.project.clone() {
        None => {
            let video = TitleCard;
            cli::new(&video, options).args(args).run()
        }
        Some(path) => {
            let storyboard = match spec::load(&path) {
                Ok(storyboard) => storyboard,
                Err(message) => {
                    eprintln!("error: {message}");
                    return ExitCode::FAILURE;
                }
            };
            let video = match scenes::build_video(storyboard) {
                Ok(video) => video,
                Err(message) => {
                    eprintln!("error: {message}");
                    return ExitCode::FAILURE;
                }
            };
            cli::new(&video, options).args(args).run()
        }
    }
}
