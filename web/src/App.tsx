import { Redirect, Route, Switch } from 'wouter'
import { AppShell } from './components/AppShell'
import { AuthRequired } from './components/AuthRequired'
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
        <Route path="/sensors/:deviceId">{(p) => <SensorDetailScreen deviceId={p.deviceId} />}</Route>
        <Route>
          <Redirect to="/" replace />
        </Route>
      </Switch>
    </AppShell>
  )
}
