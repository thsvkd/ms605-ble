export const METER_WIDTH = 16 // ms605/cli/cli.py _MONITOR_BAR_WIDTH
export const METER_TICK = 5 // ms605/cli/cli.py _MONITOR_TICK: the threshold column, the same for every bar

export type MeterCell = 'fill' | 'tick' | 'empty'

export interface MeterModel {
  cells: MeterCell[] // length = width; cells[tickAt] is always 'tick'
  over: boolean // value > threshold: the fill crosses the tick and is drawn red
}

/** Port of ms605.cli._ui.meter: the threshold tick sits at a fixed column; the fill is drawn
 *  from the signed offset value - threshold, one cell = max(|threshold| / tickAt, 1) units. */
export function meter(value: number, threshold: number, width = METER_WIDTH, tickAt = METER_TICK): MeterModel {
  if (!Number.isInteger(width) || !Number.isInteger(tickAt) || tickAt < 1 || tickAt > width - 2) {
    throw new RangeError(`tickAt must be in [1, ${width - 2}] for width=${width}, got ${tickAt}`)
  }
  const step = Math.max(Math.abs(threshold) / tickAt, 1)
  const offset = value - threshold
  const filled =
    offset > 0
      ? tickAt + 1 + Math.min(width - tickAt - 1, Math.ceil(offset / step))
      : Math.max(0, tickAt - Math.ceil(-offset / step))
  const cells = Array.from({ length: width }, (_, i): MeterCell => (i === tickAt ? 'tick' : i < filled ? 'fill' : 'empty'))
  return { cells, over: offset > 0 }
}

/** The CLI's characters, for tests: fill '█', tick '┃', empty '─'. */
export function meterPlain(m: MeterModel): string {
  return m.cells.map((c) => (c === 'fill' ? '█' : c === 'tick' ? '┃' : '─')).join('')
}
