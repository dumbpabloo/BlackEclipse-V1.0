use fframes::animation::Easing;
use fframes::{BreakLinesOpts, FFramesContext, FontQuery, Frame, Svgr, TextAlign};

use super::StoryboardScene;
use crate::theme;

pub fn render<'a>(
    scene: &'a StoryboardScene,
    frame: &mut Frame,
    ctx: &FFramesContext<'a, '_>,
) -> Svgr<'a> {
    let text = scene.spec.text.as_deref().unwrap_or("");
    let attribution = scene.spec.attribution.as_deref().unwrap_or("");
    let accent = scene.accent.as_str();

    let mark_dy: f32 =
        frame.animate(&fframes::timeline!(at 0.3 => 1.1, animate 24.0 => 0.0, Easing::EaseOut));
    let block_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.3 => 1.1, animate 0.0 => 1.0, Easing::EaseOut));
    let attribution_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.9 => 1.6, animate 0.0 => 1.0, Easing::EaseOut));

    let block = frame
        .text_break_lines(
            ctx,
            text,
            BreakLinesOpts {
                font: FontQuery {
                    family: theme::SANS,
                    size: 54,
                    weight: 300,
                    ..Default::default()
                },
                width: 1240,
                x: 340,
                y: 360,
                align: TextAlign::Left,
                fill: theme::FG,
                ..Default::default()
            },
        )
        .unwrap_or_else(|| {
            fframes::svgr!(
                <text
                    x="340" y="400" font-family={theme::SANS} font-size="54" font-weight="300"
                    fill="#f8fafc"
                >
                    {text}
                </text>
            )
        });

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            <text
                x="298" y={438.0 + mark_dy}
                font-family={theme::SANS} font-size="240" font-weight="700" fill={accent}
                opacity="0.18"
            >
                "“"
            </text>
            <g opacity={block_opacity}>{block}</g>
            <text
                x="344" y="832" font-family={theme::SANS} font-size="32" fill={theme::MUTED}
                opacity={attribution_opacity}
            >
                {attribution}
            </text>
        </svg>
    )
}
