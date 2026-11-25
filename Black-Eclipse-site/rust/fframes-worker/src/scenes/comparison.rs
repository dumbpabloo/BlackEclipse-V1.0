use fframes::animation::Easing;
use fframes::{FFramesContext, Frame, Svgr};

use super::{fit, StoryboardScene};
use crate::theme;

pub fn render<'a>(
    scene: &'a StoryboardScene,
    frame: &mut Frame,
    ctx: &FFramesContext<'a, '_>,
) -> Svgr<'a> {
    let title = scene.spec.title.as_deref().unwrap_or("");
    let accent = scene.accent.as_str();
    let left_label_raw = scene.spec.left.as_ref().map_or("", |pane| pane.label.as_str());
    let left_value_raw = scene.spec.left.as_ref().map_or("", |pane| pane.value.as_str());
    let left_detail_raw = scene
        .spec
        .left
        .as_ref()
        .and_then(|pane| pane.detail.as_deref())
        .unwrap_or("");
    let right_label_raw = scene.spec.right.as_ref().map_or("", |pane| pane.label.as_str());
    let right_value_raw = scene.spec.right.as_ref().map_or("", |pane| pane.value.as_str());
    let right_detail_raw = scene
        .spec
        .right
        .as_ref()
        .and_then(|pane| pane.detail.as_deref())
        .unwrap_or("");
    let winner = scene.spec.winner.as_deref().unwrap_or("");

    let (lx, rx, py, pw, ph) = (160.0_f32, 1020.0_f32, 330.0_f32, 740.0_f32, 560.0_f32);
    let left_stroke: &str = if winner == "left" { accent } else { theme::LINE };
    let right_stroke: &str = if winner == "right" { accent } else { theme::LINE };
    let left_width = if winner == "left" { 2.5_f32 } else { 1.5_f32 };
    let right_width = if winner == "right" { 2.5_f32 } else { 1.5_f32 };

    let left_dx: f32 =
        frame.animate(&fframes::timeline!(at 0.35 => 0.95, animate -48.0 => 0.0, Easing::EaseOut));
    let left_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.35 => 0.95, animate 0.0 => 1.0, Easing::EaseOut));
    let right_dx: f32 =
        frame.animate(&fframes::timeline!(at 0.5 => 1.1, animate 48.0 => 0.0, Easing::EaseOut));
    let right_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.5 => 1.1, animate 0.0 => 1.0, Easing::EaseOut));
    let vs_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.8 => 1.3, animate 0.0 => 1.0, Easing::EaseOut));
    let bar_left_opacity: f32 = if winner == "left" { left_opacity } else { 0.0 };
    let bar_right_opacity: f32 = if winner == "right" { right_opacity } else { 0.0 };

    let left_label = fit(frame, ctx, left_label_raw, 36, 400, pw - 96.0);
    let left_value = fit(frame, ctx, left_value_raw, 88, 600, pw - 96.0);
    let left_detail = fit(frame, ctx, left_detail_raw, 28, 300, pw - 96.0);
    let right_label = fit(frame, ctx, right_label_raw, 36, 400, pw - 96.0);
    let right_value = fit(frame, ctx, right_value_raw, 88, 600, pw - 96.0);
    let right_detail = fit(frame, ctx, right_detail_raw, 28, 300, pw - 96.0);

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            {theme::header(frame, title, accent)}
            <rect
                x={lx + 40.0 + left_dx} y="306" width="64" height="6" rx="3"
                fill={accent} opacity={bar_left_opacity}
            />
            <rect
                x={rx + 40.0 + right_dx} y="306" width="64" height="6" rx="3"
                fill={accent} opacity={bar_right_opacity}
            />
            <g opacity={left_opacity}>
                {theme::panel(lx + left_dx, py, pw, ph, left_stroke, left_width)}
                <text
                    x={lx + 48.0 + left_dx} y={py + 100.0}
                    font-family={theme::SANS} font-size="36" fill={theme::MUTED}
                >
                    {left_label}
                </text>
                <text
                    x={lx + 48.0 + left_dx} y={py + 260.0}
                    font-family={theme::SANS} font-size="88" font-weight="600" fill={theme::FG}
                >
                    {left_value}
                </text>
                <text
                    x={lx + 48.0 + left_dx} y={py + 340.0}
                    font-family={theme::SANS} font-size="28" fill={theme::DIM}
                >
                    {left_detail}
                </text>
            </g>
            <g opacity={right_opacity}>
                {theme::panel(rx + right_dx, py, pw, ph, right_stroke, right_width)}
                <text
                    x={rx + 48.0 + right_dx} y={py + 100.0}
                    font-family={theme::SANS} font-size="36" fill={theme::MUTED}
                >
                    {right_label}
                </text>
                <text
                    x={rx + 48.0 + right_dx} y={py + 260.0}
                    font-family={theme::SANS} font-size="88" font-weight="600" fill={theme::FG}
                >
                    {right_value}
                </text>
                <text
                    x={rx + 48.0 + right_dx} y={py + 340.0}
                    font-family={theme::SANS} font-size="28" fill={theme::DIM}
                >
                    {right_detail}
                </text>
            </g>
            <circle
                cx="960" cy="610" r="46" fill={theme::PANEL} stroke={theme::LINE}
                stroke-width="2" opacity={vs_opacity}
            />
            <text
                x="960" y="621" text-anchor="middle" font-family={theme::SANS} font-size="30"
                font-weight="600" fill={theme::MUTED} opacity={vs_opacity}
            >
                "VS"
            </text>
            {theme::evidence_footer(frame, scene.spec.evidence.as_ref())}
        </svg>
    )
}
