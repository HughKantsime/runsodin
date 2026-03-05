import { useState, useEffect } from 'react';
import { Webhook, Plus, Trash2, TestTube, Check, X, MessageSquare } from 'lucide-react';
import toast from 'react-hot-toast';

const API_BASE = '/api';

/**
 * Webhook Settings Component - Configure Discord/Slack webhooks
 */
export default function WebhookSettings() {
  const [webhooks, setWebhooks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newWebhook, setNewWebhook] = useState({ name: '', url: '', webhook_type: 'discord', alert_types: [] });
  const [testing, setTesting] = useState(null);
  const [testResult, setTestResult] = useState(null);

  const alertTypes = [
    { id: 'PRINT_COMPLETE', label: 'Print Complete' },
    { id: 'PRINT_FAILED', label: 'Print Failed' },
    { id: 'SPOOL_LOW', label: 'Low Spool' },
    { id: 'MAINTENANCE_OVERDUE', label: 'Maintenance Due' },
    { id: 'HMS_ERROR', label: 'Printer Errors' },
  ];

  useEffect(() => {
    loadWebhooks();
  }, []);

  const getHeaders = () => {
    return { 'Content-Type': 'application/json' };
  };

  const loadWebhooks = async () => {
    try {
      const res = await fetch(`${API_BASE}/webhooks`, { headers: getHeaders(), credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        setWebhooks(data.map(w => ({
          ...w,
          alert_types: w.alert_types ? JSON.parse(w.alert_types) : []
        })));
      }
    } catch (err) {
      console.error('Failed to load webhooks:', err);
    } finally {
      setLoading(false);
    }
  };

  const createWebhook = async () => {
    try {
      const res = await fetch(`${API_BASE}/webhooks`, {
        method: 'POST',
        headers: getHeaders(),
        credentials: 'include',
        body: JSON.stringify(newWebhook)
      });
      if (res.ok) {
        setShowAdd(false);
        setNewWebhook({ name: '', url: '', webhook_type: 'discord', alert_types: [] });
        loadWebhooks();
      }
    } catch (err) {
      toast.error('Failed to create webhook');
    }
  };

  const deleteWebhook = async (id) => {
    if (!confirm('Delete this webhook?')) return;
    try {
      await fetch(`${API_BASE}/webhooks/${id}`, { method: 'DELETE', headers: getHeaders(), credentials: 'include' });
      loadWebhooks();
    } catch (err) {
      toast.error('Failed to delete webhook');
    }
  };

  const toggleWebhook = async (id, enabled) => {
    try {
      await fetch(`${API_BASE}/webhooks/${id}`, {
        method: 'PATCH',
        headers: getHeaders(),
        credentials: 'include',
        body: JSON.stringify({ is_enabled: !enabled })
      });
      loadWebhooks();
    } catch (err) {
      toast.error('Failed to update webhook');
    }
  };

  const testWebhook = async (id) => {
    setTesting(id);
    setTestResult(null);
    try {
      const res = await fetch(`${API_BASE}/webhooks/${id}/test`, {
        method: 'POST',
        headers: getHeaders(),
        credentials: 'include',
      });
      const data = await res.json();
      setTestResult({ id, success: data.success, message: data.message });
    } catch (err) {
      setTestResult({ id, success: false, message: err.message });
    } finally {
      setTesting(null);
    }
  };

  const toggleAlertType = (type) => {
    setNewWebhook(prev => ({
      ...prev,
      alert_types: prev.alert_types.includes(type)
        ? prev.alert_types.filter(t => t !== type)
        : [...prev.alert_types, type]
    }));
  };

  if (loading) return <div className="text-[var(--brand-text-secondary)]">Loading...</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Webhook className="text-[var(--brand-primary)]" size={24} />
          <div>
            <h3 className="text-lg font-semibold">Webhooks</h3>
            <p className="text-sm text-[var(--brand-text-secondary)]">Send alerts to Discord, Slack, ntfy, or Telegram</p>
          </div>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="flex items-center gap-2 px-3 py-1.5 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary)] rounded-md text-sm transition-colors"
        >
          <Plus size={16} />
          Add Webhook
        </button>
      </div>

      {/* Add Webhook Form */}
      {showAdd && (
        <div className="p-4 bg-[var(--brand-input-bg)] rounded-md space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium mb-1">Name</label>
              <input
                type="text"
                value={newWebhook.name}
                onChange={e => setNewWebhook(prev => ({ ...prev, name: e.target.value }))}
                placeholder="My Discord Server"
                className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Type</label>
              <select
                value={newWebhook.webhook_type}
                onChange={e => setNewWebhook(prev => ({ ...prev, webhook_type: e.target.value }))}
                className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm"
              >
                <option value="discord">Discord</option>
                <option value="slack">Slack</option>
              </select>
            </div>
          </div>
          
          <div>
            <label className="block text-sm font-medium mb-1">Webhook URL</label>
            <input
              type="text"
              value={newWebhook.url}
              onChange={e => setNewWebhook(prev => ({ ...prev, url: e.target.value }))}
              placeholder={
                newWebhook.webhook_type === 'discord' ? 'https://discord.com/api/webhooks/...' :
                newWebhook.webhook_type === 'slack' ? 'https://hooks.slack.com/services/...' :
                newWebhook.webhook_type === 'ntfy' ? 'https://ntfy.sh/your-topic' :
                newWebhook.webhook_type === 'telegram' ? 'bot_token|chat_id' :
                'https://...'
              }
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm font-mono"
            />
            {newWebhook.webhook_type === 'ntfy' && (
              <p className="text-xs text-[var(--brand-text-muted)] mt-1">Enter your ntfy topic URL. Works with ntfy.sh or self-hosted instances.</p>
            )}
            {newWebhook.webhook_type === 'telegram' && (
              <p className="text-xs text-[var(--brand-text-muted)] mt-1">Format: <code className="text-[var(--brand-primary)]">bot_token|chat_id</code> — Get a bot token from <code className="text-[var(--brand-primary)]">@BotFather</code>, chat ID from <code className="text-[var(--brand-primary)]">@userinfobot</code></p>
            )}
            {newWebhook.webhook_type === 'telegram' && (
              <p className="text-xs text-[var(--brand-text-muted)] mt-1">Format: <code className="text-[var(--brand-primary)]">bot_token|chat_id</code> — Get a bot token from <code className="text-[var(--brand-primary)]">@BotFather</code>, and chat ID from <code className="text-[var(--brand-primary)]">@userinfobot</code></p>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Alert Types</label>
            <div className="flex flex-wrap gap-2">
              {alertTypes.map(type => (
                <button
                  key={type.id}
                  onClick={() => toggleAlertType(type.id)}
                  className={`px-3 py-1 rounded-md text-sm transition-colors ${
                    newWebhook.alert_types.includes(type.id)
                      ? 'bg-[var(--brand-primary)] text-white'
                      : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-input-bg)]'
                  }`}
                >
                  {type.label}
                </button>
              ))}
            </div>
            <p className="text-xs text-[var(--brand-text-muted)] mt-1">Leave empty to receive all alerts</p>
          </div>

          <div className="flex justify-end gap-2">
            <button
              onClick={() => setShowAdd(false)}
              className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] rounded-md text-sm"
            >
              Cancel
            </button>
            <button
              onClick={createWebhook}
              disabled={!newWebhook.url}
              className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary)] rounded-md text-sm disabled:opacity-50"
            >
              Create Webhook
            </button>
          </div>
        </div>
      )}

      {/* Webhook List */}
      <div className="space-y-3">
        {webhooks.length === 0 && !showAdd && (
          <div className="text-center py-8 text-[var(--brand-text-muted)]">
            <MessageSquare size={32} className="mx-auto mb-2 opacity-50" />
            <p>No webhooks configured</p>
          </div>
        )}

        {webhooks.map(webhook => (
          <div key={webhook.id} className="p-4 bg-[var(--brand-input-bg)] rounded-md">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className={`w-2 h-2 rounded-full ${webhook.is_enabled ? 'bg-green-500' : 'bg-[var(--brand-input-bg)]'}`} />
                <div>
                  <div className="font-medium">{webhook.name}</div>
                  <div className="text-xs text-[var(--brand-text-muted)] capitalize">{webhook.webhook_type}</div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {testResult?.id === webhook.id && (
                  <span className={`text-xs ${testResult.success ? 'text-green-400' : 'text-red-400'}`}>
                    {testResult.success ? <Check size={14} className="inline" /> : <X size={14} className="inline" />}
                    {testResult.message}
                  </span>
                )}
                <button
                  onClick={() => testWebhook(webhook.id)}
                  disabled={testing === webhook.id}
                  className="p-2 text-[var(--brand-text-secondary)] hover:text-[var(--brand-primary)] hover:bg-[var(--brand-input-bg)] rounded-md transition-colors"
                  title="Test"
                >
                  <TestTube size={16} className={testing === webhook.id ? 'animate-pulse' : ''} />
                </button>
                <button
                  onClick={() => toggleWebhook(webhook.id, webhook.is_enabled)}
                  className={`px-3 py-1 rounded-md text-xs ${
                    webhook.is_enabled ? 'bg-green-900/50 text-green-400' : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]'
                  }`}
                >
                  {webhook.is_enabled ? 'Enabled' : 'Disabled'}
                </button>
                <button
                  onClick={() => deleteWebhook(webhook.id)}
                  className="p-2 text-[var(--brand-text-secondary)] hover:text-red-400 hover:bg-red-900/50 rounded-md transition-colors"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
            {webhook.alert_types?.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {webhook.alert_types.map(type => (
                  <span key={type} className="px-2 py-0.5 bg-[var(--brand-input-bg)] rounded-md text-xs text-[var(--brand-text-secondary)]">
                    {alertTypes.find(t => t.id === type)?.label || type}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
