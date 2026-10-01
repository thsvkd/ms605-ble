import { Redirect, Route, Switch } from 'wouter'
import { AppShell } from './components/AppShell'
import { AuthRequired } from './components/AuthRequired'
import { BulkEditScreen } from './screens/BulkEdit'
import { CalibrateScreen } from './screens/Calibrate'
import { DashboardScreen } from './screens/Dashboard'
import { GatherScreen } from './screens/Gather'
import { MonitorScreen } from './screens/Monitor'
import { SensorDetailScreen } from './screens/SensorDetail'
import { useStore } from './store/store'

export function App() {
  const unauthorized = useStore((s) => s.conn === 'unauthorized')
  if (unauthorized) return <AuthRequired />
  return (
    <AppShell>
      <Switch>
        <Route path="/" component={DashboardScreen} />
        <Route path="/gather" component={GatherScreen} />
        <Route path="/monitor" component={MonitorScreen} />
        <Route path="/calibrate" component={CalibrateScreen} />
        <Route path="/bulk" component={BulkEditScreen} />
        <Route path="/sensors/:deviceId">{(p) => <SensorDetailScreen deviceId={p.deviceId} />}</Route>
        <Route path="/sensors/:deviceId/settings">
          {(p) => <SensorDetailScreen deviceId={p.deviceId} tab="settings" />}
        </Route>
        <Route path="/sensors/:deviceId/advanced">
          {(p) => <SensorDetailScreen deviceId={p.deviceId} tab="advanced" />}
        </Route>
        <Route path="/sensors/:deviceId/history">
          {(p) => <SensorDetailScreen deviceId={p.deviceId} tab="history" />}
        </Route>
        <Route>
          <Redirect to="/" replace />
        </Route>
      </Switch>
    </AppShell>
  )
}
