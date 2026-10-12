import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

// Project name + task type, shared by the team dialogs that create a project inside a team.
export default function ProjectNameFields({ name, onName, taskType, onTaskType, testIdPrefix }) {
  return (
    <div className="space-y-4">
      <div>
        <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Project name</Label>
        <Input
          value={name}
          onChange={(e) => onName(e.target.value)}
          placeholder="Warehouse defects"
          className="rounded-sm bg-transparent border-[#27272A] focus-visible:ring-primary"
          data-testid={`${testIdPrefix}-project-name-input`}
        />
      </div>
      <div>
        <Label className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground mb-2 block">Task type</Label>
        <Select value={taskType} onValueChange={onTaskType}>
          <SelectTrigger className="rounded-sm bg-transparent border-[#27272A]" data-testid={`${testIdPrefix}-project-tasktype-select`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="bg-[#121212] border-[#27272A] rounded-sm">
            <SelectItem value="object_detection">Object Detection</SelectItem>
            <SelectItem value="classification">Classification</SelectItem>
            <SelectItem value="segmentation">Instance Segmentation</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
