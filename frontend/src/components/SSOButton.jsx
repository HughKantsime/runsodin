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
    <div className="mt-4">
      <div className="relative">
        <div className="absolute inset-0 flex items-center">
          <div className="w-full border-t border-farm-700"></div>
        </div>
        <div className="relative flex justify-center text-sm">
          <span className="px-2 bg-farm-900 text-farm-400">or</span>
        </div>
      </div>
      
      <button
        onClick={handleSSO}
        className="mt-4 w-full flex items-center justify-center gap-2 px-4 py-2.5 
                   bg-[#0078d4] hover:bg-[#106ebe] text-white rounded-lg 
                   transition-colors font-medium"
      >
        <Shield size={18} />
        {config.display_name || 'Sign in with SSO'}
      </button>
    </div>
  );
}
