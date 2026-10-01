import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { connectWs } from './api/ws'
import { applyTheme, readTheme } from './theme'
import './styles/tokens.css'
import './styles/global.css'

applyTheme(readTheme())
connectWs()

const root = document.getElementById('root')
if (!root) throw new Error('#root missing from index.html')
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
