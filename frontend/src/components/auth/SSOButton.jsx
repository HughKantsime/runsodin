import { useState, useEffect } from 'react';
import { Shield } from 'lucide-react';

/**
 * SSO Login Button - Shows "Sign in with Microsoft" if OIDC is enabled
 */
export default function SSOButton() {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check if SSO is enabled
    fetch('/api/auth/oidc/config')
      .then(res => res.json())
      .then(data => {
        setConfig(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading || !config?.enabled) {
    return null;
  }

  const handleSSO = () => {
    // Redirect to OIDC login endpoint
    window.location.href = '/api/auth/oidc/login';
  };

  return (
    <div>
      <button
        onClick={handleSSO}
        className="w-full flex items-center justify-center gap-2 px-4 py-2.5
                   bg-transparent border border-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)] rounded-md
                   transition-colors font-medium"
        style={{ color: 'var(--brand-text-secondary)' }}
      >
        <Shield size={18} />
        {config.display_name || 'Sign in with SSO'}
      </button>
    </div>
  );
}
