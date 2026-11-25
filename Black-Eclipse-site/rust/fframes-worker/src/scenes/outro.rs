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
    let duration = scene.spec.duration_seconds;
    let fade_start = (duration - 0.8).max(0.0);

    let title_dy: f32 =
        frame.animate(&fframes::timeline!(at 0.3 => 1.1, animate 30.0 => 0.0, Easing::EaseOut));
    let title_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.3 => 1.1, animate 0.0 => 1.0, Easing::EaseOut));
    let underline_width: f32 =
        frame.animate(&fframes::timeline!(at 0.4 => 1.15, animate 1.0 => 120.0, Easing::EaseOut));
    let underline_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.4 => 1.15, animate 0.0 => 1.0, Easing::EaseOut));
    let subtitle_dy: f32 =
        frame.animate(&fframes::timeline!(at 0.9 => 1.6, animate 20.0 => 0.0, Easing::EaseOut));
    let subtitle_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.9 => 1.6, animate 0.0 => 1.0, Easing::EaseOut));
    let fade: f32 =
        frame.animate(&fframes::timeline!(at {fade_start} => {duration}, animate 0.0 => 1.0, Easing::EaseIn));

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            <text
                x="960" y={520.0 + title_dy}
                text-anchor="middle" font-family={theme::SANS} font-size="96" font-weight="600"
                fill={theme::FG} opacity={title_opacity}
            >
                {title}
            </text>
            <rect
                x="900" y="566" width={underline_width} height="6" rx="3"
                fill={accent} opacity={underline_opacity}
            />
            <text
                x="960" y={648.0 + subtitle_dy}
                text-anchor="middle" font-family={theme::SANS} font-size="36" font-weight="300"
                fill={theme::MUTED} opacity={subtitle_opacity}
            >
                {subtitle}
            </text>
            <rect width="1920" height="1080" fill="#000000" opacity={fade} />
        </svg>
    )
}
