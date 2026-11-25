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
    let symbol = scene.spec.symbol.as_deref().unwrap_or("");
    let accent = scene.accent.as_str();
    let candles = scene.spec.candles.as_slice();

    let (px, py, pw, ph) = (160.0_f32, 300.0_f32, 1600.0_f32, 620.0_f32);
    let inner_x0 = px + 56.0;
    let inner_w = pw - 112.0;
    let top = py + 96.0;
    let bottom = py + ph - 44.0;
    let inner_h = bottom - top;

    let min_low = candles.iter().fold(f64::INFINITY, |acc, candle| acc.min(candle.l));
    let max_high = candles.iter().fold(f64::NEG_INFINITY, |acc, candle| acc.max(candle.h));
    let (mut lo, mut hi) = (min_low, max_high);
    if hi - lo <= 0.0 {
        lo -= 0.5;
        hi += 0.5;
    }
    let pad = (hi - lo) * 0.04;
    let (lo, hi) = (lo - pad, hi + pad);
    let span = hi - lo;
    let y_of = |value: f64| -> f32 {
        (f64::from(top) + (hi - value) / span * f64::from(inner_h)) as f32
    };

    let slot = inner_w / candles.len() as f32;
    let body_w = (slot * 0.55).clamp(6.0, 26.0);
    let mut candle_shapes: Vec<Svgr> = Vec::new();
    for (index, candle) in candles.iter().enumerate() {
        let cx = inner_x0 + slot * (index as f32 + 0.5);
        let color = if candle.c >= candle.o { theme::GREEN } else { theme::RED };
        let wick_top = y_of(candle.h);
        let wick_bottom = y_of(candle.l);
        let body_top = y_of(candle.o.max(candle.c));
        let body_bottom = y_of(candle.o.min(candle.c));
        let body_h = (body_bottom - body_top).max(3.0);
        candle_shapes.push(fframes::svgr!(
            <g>
                <line x1={cx} y1={wick_top} x2={cx} y2={wick_bottom} stroke={color} stroke-width="2.5" />
                <rect x={cx - body_w / 2.0} y={body_top} width={body_w} height={body_h} rx="1.5" fill={color} />
            </g>
        ));
    }

    let last_y = candles.last().map_or(top, |candle| y_of(candle.c));
    let grid_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.7 => 1.4, animate 0.0 => 1.0, Easing::EaseOut));
    let panel_opacity: f32 =
        frame.animate(&fframes::timeline!(at 0.15 => 0.7, animate 0.0 => 1.0, Easing::EaseOut));
    let reveal_w: f32 = frame.animate(&fframes::timeline!(
        at 0.5 => 2.2, animate 1.0 => {inner_w + 24.0}, Easing::Linear,
    ));

    let clip_id = format!("sb-reveal-{}", scene.index);
    let clip_url = format!("url(#{clip_id})");
    let chip = format!(
        "{} · {} – {}",
        symbol,
        theme::fmt_number(min_low),
        theme::fmt_number(max_high)
    );

    let mut grid: Vec<Svgr> = Vec::new();
    for step in 0..=3 {
        let grid_y = top + inner_h * (step as f32 / 3.0);
        grid.push(fframes::svgr!(
            <line
                x1={inner_x0} y1={grid_y} x2={inner_x0 + inner_w} y2={grid_y}
                stroke="#16213a" stroke-width="1"
            />
        ));
    }

    fframes::svgr!(
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
            {theme::background(scene.index, &scene.accent)}
            {theme::header(frame, title, accent)}
            <g opacity={panel_opacity}>
                {theme::panel(px, py, pw, ph, theme::LINE, 1.5)}
                <text x={inner_x0} y={py + 54.0} font-family="Consolas" font-size="26" fill={theme::DIM}>
                    {chip}
                </text>
                <g opacity={grid_opacity}>{grid}</g>
            </g>
            <defs>
                <clipPath id={clip_id}>
                    <rect x={inner_x0 - 12.0} y={top - 12.0} width={reveal_w} height={inner_h + 24.0} />
                </clipPath>
            </defs>
            <g clip-path={clip_url}>
                {candle_shapes}
                <line
                    x1={inner_x0} y1={last_y} x2={inner_x0 + inner_w} y2={last_y}
                    stroke={accent} stroke-width="1.5" stroke-dasharray="6 6" opacity="0.55"
                />
            </g>
            {theme::evidence_footer(frame, scene.spec.evidence.as_ref())}
        </svg>
    )
}
