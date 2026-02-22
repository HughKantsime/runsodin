import { useState, useEffect } from 'react';
import { Shield, Save, Eye, EyeOff, ExternalLink, CheckCircle, XCircle } from 'lucide-react';

/**
 * OIDC/SSO Configuration Tab for Settings page
 * Admin-only - configure Microsoft Entra ID SSO
 */
export default function OIDCSettings() {
  const [config, setConfig] = useState({
    is_enabled: false,
    display_name: 'Microsoft Entra ID',
    client_id: '',
    tenant_id: '',
    discovery_url: '',
    scopes: 'openid profile email',
    auto_create_users: true,
    default_role: 'operator',
    has_client_secret: false,
  });
  const [clientSecret, setClientSecret] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      ;
      const res = await fetch('/api/admin/oidc', {
      });
      if (res.ok) {
        const data = await res.json();
        if (data.configured !== false) {
          setConfig(data);
        }
      }
    } catch (err) {
      console.error('Failed to load OIDC config:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);

    try {
      ;
      const payload = { ...config };
      
      // Only include secret if changed
      if (clientSecret) {
        payload.client_secret = clientSecret;
      }

      const res = await fetch('/api/admin/oidc', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        setMessage({ type: 'success', text: 'OIDC configuration saved' });
        setClientSecret('');
        loadConfig();
      } else {
        throw new Error('Failed to save');
      }
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to save configuration' });
    } finally {
      setSaving(false);
    }
  };

  const handleChange = (field, value) => {
    setConfig(prev => ({ ...prev, [field]: value }));
  };

  if (loading) {
    return <div className="p-4 text-farm-400">Loading...</div>;
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Shield className="text-print-500" size={24} />
        <div>
          <h3 className="text-lg font-semibold">Single Sign-On (SSO)</h3>
          <p className="text-sm text-farm-400">
            Configure Microsoft Entra ID for enterprise authentication
          </p>
        </div>
      </div>

      {/* Enable Toggle */}
      <div className="flex items-center justify-between p-4 bg-farm-800 rounded-lg">
        <div>
          <div className="font-medium">Enable SSO</div>
          <div className="text-sm text-farm-400">
            Show "Sign in with Microsoft" on login page
          </div>
        </div>
        <button
          onClick={() => handleChange('is_enabled', !config.is_enabled)}
          className={`relative w-12 h-6 rounded-full transition-colors ${
            config.is_enabled ? 'bg-print-600' : 'bg-farm-700'
          }`}
        >
          <div className={`absolute top-1 w-4 h-4 bg-white rounded-full transition-transform ${
            config.is_enabled ? 'left-7' : 'left-1'
          }`} />
        </button>
      </div>

      {/* Configuration Form */}
      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Display Name</label>
          <input
            type="text"
            value={config.display_name || ''}
            onChange={(e) => handleChange('display_name', e.target.value)}
            placeholder="Microsoft Entra ID"
            className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
          />
          <p className="text-xs text-farm-500 mt-1">Shown on login button</p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Client ID</label>
            <input
              type="text"
              value={config.client_id || ''}
              onChange={(e) => handleChange('client_id', e.target.value)}
              placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm font-mono"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Tenant ID</label>
            <input
              type="text"
              value={config.tenant_id || ''}
              onChange={(e) => handleChange('tenant_id', e.target.value)}
              placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm font-mono"
            />
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium mb-1">
            Client Secret
            {config.has_client_secret && (
              <span className="ml-2 text-xs text-green-500">● Configured</span>
            )}
          </label>
          <div className="relative">
            <input
              type={showSecret ? 'text' : 'password'}
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              placeholder={config.has_client_secret ? '••••••••••••••••' : 'Enter client secret'}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 pr-10 text-sm font-mono"
            />
            <button
              type="button"
              onClick={() => setShowSecret(!showSecret)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-farm-400 hover:text-farm-300"
            >
              {showSecret ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
          <p className="text-xs text-farm-500 mt-1">Leave blank to keep existing secret</p>
        </div>

        <div>
          <label className="block text-sm font-medium mb-1">
            Discovery URL
            <span className="ml-1 text-xs text-farm-500">(optional)</span>
          </label>
          <input
            type="text"
            value={config.discovery_url || ''}
            onChange={(e) => handleChange('discovery_url', e.target.value)}
            placeholder="https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
            className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm font-mono text-xs"
          />
          <p className="text-xs text-farm-500 mt-1">
            Leave blank for commercial Azure. For GCC High, use: 
            <code className="ml-1 text-farm-400">https://login.microsoftonline.us/...</code>
          </p>
        </div>

        <div>
          <label className="block text-sm font-medium mb-1">Scopes</label>
          <input
            type="text"
            value={config.scopes || ''}
            onChange={(e) => handleChange('scopes', e.target.value)}
            placeholder="openid profile email"
            className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm font-mono"
          />
        </div>

        {/* User Provisioning */}
        <div className="p-4 bg-farm-800 rounded-lg space-y-4">
          <h4 className="font-medium">User Provisioning</h4>
          
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm">Auto-create users</div>
              <div className="text-xs text-farm-400">
                Create accounts for new SSO users automatically
              </div>
            </div>
            <button
              onClick={() => handleChange('auto_create_users', !config.auto_create_users)}
              className={`relative w-10 h-5 rounded-full transition-colors ${
                config.auto_create_users ? 'bg-print-600' : 'bg-farm-700'
              }`}
            >
              <div className={`absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform ${
                config.auto_create_users ? 'left-5' : 'left-0.5'
              }`} />
            </button>
          </div>

          <div>
            <label className="block text-sm mb-1">Default role for new users</label>
            <select
              value={config.default_role || 'operator'}
              onChange={(e) => handleChange('default_role', e.target.value)}
              className="bg-farm-700 border border-farm-600 rounded-lg px-3 py-2 text-sm"
            >
              <option value="viewer">Viewer</option>
              <option value="operator">Operator</option>
              <option value="admin">Admin</option>
            </select>
          </div>
        </div>
      </div>

      {/* Message */}
      {message && (
        <div className={`flex items-center gap-2 p-3 rounded-lg ${
          message.type === 'success' 
            ? 'bg-green-900/30 text-green-400' 
            : 'bg-red-900/30 text-red-400'
        }`}>
          {message.type === 'success' ? <CheckCircle size={16} /> : <XCircle size={16} />}
          {message.text}
        </div>
      )}

      {/* Save Button */}
      <div className="flex justify-end">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 
                     rounded-lg transition-colors disabled:opacity-50"
        >
          <Save size={16} />
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>

      {/* Help Link */}
      <div className="text-sm text-farm-400 border-t border-farm-800 pt-4">
        <a 
          href="https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1 text-print-400 hover:text-print-300"
        >
          <ExternalLink size={14} />
          How to register an app in Microsoft Entra ID
        </a>
      </div>
    </div>
  );
}
