import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { appTheme } from './theme'
import { AuthProvider } from './auth/AuthContext'
import { AdminAuthProvider } from './auth/AdminAuthContext'
import { AppLayout } from './layouts/AppLayout'
import { AdminHome, AdminLayout } from './layouts/AdminLayout'
import { LoginPage } from './pages/LoginPage'
import { RegisterPage } from './pages/RegisterPage'
import { ChatPage } from './pages/ChatPage'
import { KeysPage } from './pages/KeysPage'
import { WalletPage } from './pages/WalletPage'
import { UsagePage } from './pages/UsagePage'
import { OrgsPage } from './pages/OrgsPage'
import { RechargePage } from './pages/RechargePage'
import { ReportsPage } from './pages/ReportsPage'
import { KbListPage } from './pages/kb/KbListPage'
import { KbDetailPage } from './pages/kb/KbDetailPage'
import { KbChunksPage } from './pages/kb/KbChunksPage'
import { AdminLoginPage } from './pages/admin/AdminLoginPage'
import { ChannelsPage } from './pages/admin/ChannelsPage'
import { ModelsPage } from './pages/admin/ModelsPage'
import { UsersPage } from './pages/admin/UsersPage'
import { RolesPage } from './pages/admin/RolesPage'
import { WalletsPage } from './pages/admin/WalletsPage'
import { StatsPage } from './pages/admin/StatsPage'
import { AdminOrgsPage } from './pages/admin/OrgsPage'
import { AdminPaymentsPage } from './pages/admin/PaymentsPage'
import { AdminReportsPage } from './pages/admin/ReportsPage'
import { AdminKbPage } from './pages/admin/AdminKbPage'
import { AdminAccountsPage } from './pages/admin/AdminAccountsPage'

export default function App() {
  return (
    <ConfigProvider locale={zhCN} theme={appTheme} wave={{ disabled: true }}>
      <AuthProvider>
        <AdminAuthProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/register" element={<RegisterPage />} />
              <Route element={<AppLayout />}>
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/kb" element={<KbListPage />} />
                <Route path="/kb/:kbId" element={<KbDetailPage />} />
                <Route path="/kb/:kbId/documents/:docId" element={<KbChunksPage />} />
                <Route path="/keys" element={<KeysPage />} />
                <Route path="/wallet" element={<WalletPage />} />
                <Route path="/recharge" element={<RechargePage />} />
                <Route path="/orgs" element={<OrgsPage />} />
                <Route path="/usage" element={<UsagePage />} />
                <Route path="/reports" element={<ReportsPage />} />
              </Route>
              <Route path="/admin/login" element={<AdminLoginPage />} />
              <Route path="/admin" element={<AdminLayout />}>
                <Route index element={<AdminHome />} />
                <Route path="channels" element={<ChannelsPage />} />
                <Route path="models" element={<ModelsPage />} />
                <Route path="users" element={<UsersPage />} />
                <Route path="roles" element={<RolesPage />} />
                <Route path="kb" element={<AdminKbPage />} />
                <Route path="orgs" element={<AdminOrgsPage />} />
                <Route path="wallets" element={<WalletsPage />} />
                <Route path="payments" element={<AdminPaymentsPage />} />
                <Route path="stats" element={<StatsPage />} />
                <Route path="reports" element={<AdminReportsPage />} />
                <Route path="accounts" element={<AdminAccountsPage />} />
              </Route>
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
          </BrowserRouter>
        </AdminAuthProvider>
      </AuthProvider>
    </ConfigProvider>
  )
}
