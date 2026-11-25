use fframes::animation::Easing;
use fframes::{FFramesContext, Frame, Svgr};

use super::StoryboardScene;
use crate::theme;

pub fn render<'a>(
    scene: &'a StoryboardScene,
    frame: &mut Frame,
    _ctx: &FFramesContext<'a, '_>,
) -> Svgr<'a> {
    let title = scene.spec.title.as_deref().unwrap_or("");
    let subtitle = scene.spec.subtitle.as_deref().unwrap_or("");
    let accent = scene.accent.as_str();

    let bar_x: f32 = frame.animate(&fframes::timeline!(
        at 0.2 => 1.0, animate -320.0 => 0.0, Easing::CubicBezier(0.22, 1.0, 0.36, 1.0),
    ));
    let title_dy: f32 =
        frame.animate(&fframes::timeline!(at 0.4 => 1.2, animate 40.0 => 0.0, Easing::EaseOut));
    let title_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.4 => 1.2, animate 0.0 => 1.0, Easing::EaseOut));
    let subtitle_dy: f32 =
        frame.animate(&fframes::timeline!(at 1.0 => 1.8, animate 24.0 => 0.0, Easing::EaseOut));
    let subtitle_opacity: f32 =
        frame.animate(&fframes::timeline!(at 1.0 => 1.8, animate 0.0 => 1.0, Easing::EaseOut));
    let rule_width: f32 =
        frame.animate(&fframes::timeline!(at 0.7 => 1.6, animate 1.0 => 520.0, Easing::EaseInOut));
    let rule_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.7 => 1.2, animate 0.0 => 1.0, Easing::EaseOut));

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            <rect x={160.0 + bar_x} y="548" width="128" height="8" rx="4" fill={accent} />
            <text
                x="160" y={500.0 + title_dy}
                font-family="Segoe UI" font-size="118" font-weight="600" fill={theme::FG}
                opacity={title_opacity}
            >
                {title}
            </text>
            <text
                x="164" y={616.0 + subtitle_dy}
                font-family="Segoe UI" font-size="40" font-weight="300" fill={theme::MUTED}
                opacity={subtitle_opacity}
            >
                {subtitle}
            </text>
            <rect
                x="164" y="700" width={rule_width} height="3" rx="1.5"
                fill={accent} opacity={rule_opacity}
            />
        </svg>
    )
}
