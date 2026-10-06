import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Router } from 'wouter'
import { App } from './App'
import { connectWs } from './api/ws'
import { guardNav } from './navGuard'
import { applyTheme, readTheme } from './theme'
import { initializeLocale } from './strings'
import './styles/tokens.css'
import './styles/global.css'

initializeLocale()
applyTheme(readTheme())
connectWs()

const root = document.getElementById('root')
if (!root) throw new Error('#root missing from index.html')
createRoot(root).render(
  <StrictMode>
    <Router aroundNav={guardNav}>
      <App />
    </Router>
  </StrictMode>,
)
