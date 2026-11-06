import { fetchAPI } from './client'

export interface TrainingQuestion { prompt: string; choices: string[]; correct?: number }
export interface TrainingPrinter { id: number; name: string; machine_type: string | null; model: string | null }
export interface TrainingRecord { quiz_attempts?: number; cooldown_until?: string | null; completed_at: string | null; expires_at: string | null; quiz_score: number | null; revoked_at: string | null }
export interface TrainingDefinition {
  school_wide?: boolean;
  name: string; description: string; scope_kind: 'printer_group' | 'machine_type' | 'model'; scope_value: string;
  quiz: TrainingQuestion[]; pass_mark: number; instructor_required: boolean; expiry_days: number | null; active: boolean;
}
export interface Training extends TrainingDefinition {
  matched_printers?: Pick<TrainingPrinter, 'id' | 'name'>[];
  id: string; revision: number; edit_version: number; status: string; can_manage: boolean; can_edit: boolean; record: TrainingRecord | null;
}
export interface TrainingStudent { user_id: number; username: string; active: boolean; status: string; record: TrainingRecord | null }
const command = (revision: number) => ({ command_id: crypto.randomUUID(), revision })
export const educationTraining = {
  list: (center: number): Promise<{ items: Training[]; can_manage: boolean; can_manage_school_wide?: boolean; printers?: TrainingPrinter[]; school_printers?: TrainingPrinter[] }> => fetchAPI(`/education/cost-centers/${center}/trainings`),
  create: (center: number, data: TrainingDefinition) => fetchAPI(`/education/cost-centers/${center}/trainings`, { method: 'POST', body: JSON.stringify({ ...data, command_id: crypto.randomUUID() }) }),
  update: (training: Training, data: TrainingDefinition) => fetchAPI(`/education/trainings/${training.id}`, { method: 'PUT', body: JSON.stringify({ ...data, ...command(training.revision), edit_version: training.edit_version }) }),
  quiz: (training: Training, answers: number[]): Promise<{ status: string; quiz_score: number }> => fetchAPI(`/education/trainings/${training.id}/quiz`, { method: 'POST', body: JSON.stringify({ ...command(training.revision), answers }) }),
  roster: (id: string): Promise<{ items: TrainingStudent[] }> => fetchAPI(`/education/trainings/${id}/records`),
  assess: (training: Training, student: number, action: 'signoff' | 'revoke') => fetchAPI(`/education/trainings/${training.id}/students/${student}/${action}`, { method: 'POST', body: JSON.stringify({ ...command(training.revision), reason: action === 'signoff' ? 'instructor_assessment' : 'safety_review' }) }),
}
