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
    let metrics = scene.spec.metrics.as_slice();
    let count = metrics.len() as f32;
    let gap = 32.0_f32;
    let card_w = (theme::W - 2.0 * theme::MARGIN - gap * (count - 1.0)) / count;
    let value_size = if count <= 2.0 {
        76
    } else if count <= 3.0 {
        64
    } else {
        54
    };
    let top = 360.0_f32;
    let card_h = 340.0_f32;

    let mut cards: Vec<Svgr> = Vec::new();
    for (index, metric) in metrics.iter().enumerate() {
        let x = theme::MARGIN + index as f32 * (card_w + gap);
        let delay = 0.45_f32 + index as f32 * 0.12;
        let label = fit(frame, ctx, &metric.label, 28, 400, card_w - 72.0);
        let value = fit(frame, ctx, &metric.value, value_size, 600, card_w - 72.0);
        let delta = metric.delta.as_deref().unwrap_or("");
        let delta_color = if delta.starts_with('+') {
            theme::GREEN
        } else if delta.starts_with('-') {
            theme::RED
        } else {
            theme::MUTED
        };
        let dy: f32 = frame.animate(&fframes::timeline!(
            at {delay} => {delay + 0.5}, animate 26.0 => 0.0, Easing::EaseOut,
        ));
        let opacity: f32 = frame.animate(&fframes::timeline!(
            at {delay} => {delay + 0.5}, animate 0.0 => 1.0, Easing::EaseOut,
        ));
        cards.push(fframes::svgr!(
            <g opacity={opacity}>
                <rect
                    x={x} y={top + dy} width={card_w} height={card_h} rx="24"
                    fill={theme::PANEL} stroke={theme::LINE} stroke-width="1.5"
                />
                <text
                    x={x + 36.0} y={top + 84.0 + dy}
                    font-family="Segoe UI" font-size="28" fill={theme::MUTED}
                >
                    {label}
                </text>
                <text
                    x={x + 36.0} y={top + 200.0 + dy}
                    font-family="Segoe UI" font-size={value_size} font-weight="600" fill={theme::FG}
                >
                    {value}
                </text>
                <text
                    x={x + 36.0} y={top + 284.0 + dy}
                    font-family={theme::MONO} font-size="32" fill={delta_color}
                >
                    {delta}
                </text>            </g>
        ));
    }

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            {theme::header(frame, title, accent)}
            {cards}
            {theme::evidence_footer(frame, scene.spec.evidence.as_ref())}
        </svg>
    )
}
