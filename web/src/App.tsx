import { Outlet } from 'react-router-dom'
import { Sidebar, MobileTopBar } from './components/Sidebar'

export default function App() {
  return (
    <div className="h-full flex flex-col md:flex-row bg-bg text-text">
      {/* Mobile-only top bar (brand + tab nav). Hidden on md+. */}
      <MobileTopBar />
      {/* Desktop sidebar. Hidden on mobile. */}
      <Sidebar />
      <main className="flex-1 overflow-hidden flex flex-col">
        <Outlet />
      </main>
    </div>
  )
}
