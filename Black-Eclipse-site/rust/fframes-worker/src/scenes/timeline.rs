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
    let events = scene.spec.events.as_slice();
    let count = events.len() as f32;
    let (x0, x1, axis_y) = (240.0_f32, 1680.0_f32, 660.0_f32);
    let span = x1 - x0;
    let slot = span / count;

    let axis_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.3 => 0.9, animate 0.0 => 1.0, Easing::EaseOut));
    let progress_w: f32 = frame.animate(&fframes::timeline!(
        at 0.5 => 1.8, animate 1.0 => {span + 1.0}, Easing::EaseInOut,
    ));

    let mut nodes: Vec<Svgr> = Vec::new();
    for (index, event) in events.iter().enumerate() {
        let x = x0 + (index as f32 + 0.5) * slot;
        let delay = 0.5_f32 + index as f32 * 0.15;
        let radius: f32 = frame.animate(&fframes::timeline!(
            at {delay} => {delay + 0.4}, animate 0.5 => 11.0, Easing::EaseOut,
        ));
        let opacity: f32 = frame.animate(&fframes::timeline!(
            at {delay} => {delay + 0.4}, animate 0.0 => 1.0, Easing::EaseOut,
        ));
        let label_dy: f32 = frame.animate(&fframes::timeline!(
            at {delay + 0.1} => {delay + 0.5}, animate 12.0 => 0.0, Easing::EaseOut,
        ));
        let label_opacity: f32 = frame.animate(&fframes::timeline!(
            at {delay + 0.1} => {delay + 0.5}, animate 0.0 => 1.0, Easing::EaseOut,
        ));
        let label = fit(frame, ctx, &event.label, 36, 600, slot - 24.0);
        let detail = fit(frame, ctx, event.detail.as_deref().unwrap_or(""), 25, 300, slot - 24.0);
        nodes.push(fframes::svgr!(
            <g opacity={opacity}>
                <circle cx={x} cy={axis_y} r={radius} fill={accent} />
                <circle
                    cx={x} cy={axis_y} r={radius * 1.6} fill="none"
                    stroke={accent} stroke-width="1.5" opacity="0.25"
                />
                <line
                    x1={x} y1={axis_y + 16.0} x2={x} y2={axis_y + 40.0}
                    stroke={theme::LINE} stroke-width="2"
                />
                <text
                    x={x} y={axis_y - 56.0 + label_dy}
                    text-anchor="middle" font-family="Segoe UI" font-size="36" font-weight="600"
                    fill={theme::FG} opacity={label_opacity}
                >
                    {label}
                </text>
                <text
                    x={x} y={axis_y + 84.0 + label_dy}
                    text-anchor="middle" font-family="Segoe UI" font-size="25" fill={theme::DIM}
                    opacity={label_opacity}
                >
                    {detail}
                </text>
            </g>
        ));
    }

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            {theme::header(frame, title, accent)}
            <line
                x1={x0} y1={axis_y} x2={x1} y2={axis_y}
                stroke={theme::LINE} stroke-width="3" opacity={axis_opacity}
            />
            <rect
                x={x0} y={axis_y - 1.5} width={progress_w} height="3" rx="1.5"
                fill={accent} opacity={axis_opacity}
            />
            {nodes}
        </svg>
    )
}
