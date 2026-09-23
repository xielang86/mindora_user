"""
Heart Rate Sequence Generator - Simulates falling asleep pattern
"""
import json
from datetime import datetime, timedelta
from typing import List, Tuple, Dict
import random


class HeartRateGenerator:
    """Generate realistic heart rate sequences simulating falling asleep"""
    
    def __init__(self, 
                 start_time: int, 
                 duration_seconds: int, 
                 initial_heart_rate: int,
                 interval_seconds: int = 5):
        """
        Args:
            start_time: Unix timestamp (seconds) for sequence start
            duration_seconds: Total duration of the sequence in seconds
            initial_heart_rate: Heart rate (BPM) at the beginning
            interval_seconds: Sampling interval (default 5 seconds)
        """
        self.start_time = start_time
        self.duration_seconds = duration_seconds
        self.initial_heart_rate = initial_heart_rate
        self.interval_seconds = interval_seconds
    
    def generate(self) -> List[Dict[str, float]]:
        """
        Generate heart rate sequence with falling asleep pattern
        
        Returns:
            List of dicts with 'timestamp' and 'heart_rate' keys
        """
        sequence = []
        num_points = self.duration_seconds // self.interval_seconds
        
        for i in range(num_points):
            timestamp = self.start_time + (i * self.interval_seconds)
            
            # Calculate progress (0.0 to 1.0)
            progress = i / max(num_points - 1, 1)
            
            # Simulate falling asleep: gradual decrease with slight variations
            # Target heart rate when fully asleep (lower than initial)
            target_hr = self.initial_heart_rate * 0.65
            
            # Non-linear decrease: faster at start, slower towards end
            base_hr = self.initial_heart_rate - (self.initial_heart_rate - target_hr) * (progress ** 1.2)
            
            # Add natural variations (small fluctuations)
            # Smaller variations as person gets sleepier
            variation_range = (1 - progress) * 3
            variation = random.uniform(-variation_range, variation_range)
            
            # Add occasional larger fluctuations (similar to real heart rate)
            if random.random() < 0.05:  # 5% chance
                variation += random.uniform(-5, 5)
            
            heart_rate = round(max(40, base_hr + variation), 1)  # Min 40 BPM
            
            sequence.append({
                "timestamp": timestamp,
                "heart_rate": heart_rate
            })
        
        return sequence
    
    def generate_as_tuples(self) -> List[Tuple[int, float]]:
        """Generate as list of (timestamp, heart_rate) tuples"""
        sequence = self.generate()
        return [(item["timestamp"], item["heart_rate"]) for item in sequence]


def generate_heart_rate_sequence(start_time: int,
                                 duration_seconds: int,
                                 initial_heart_rate: int,
                                 interval_seconds: int = 5) -> List[Dict[str, float]]:
    """
    Convenience function to generate heart rate sequence
    
    Args:
        start_time: Unix timestamp (seconds)
        duration_seconds: Duration in seconds
        initial_heart_rate: Starting heart rate in BPM
        interval_seconds: Sampling interval in seconds (default 5)
    
    Returns:
        List of dicts with timestamp and heart_rate
    """
    generator = HeartRateGenerator(start_time, duration_seconds, initial_heart_rate, interval_seconds)
    return generator.generate()


# Example usage
if __name__ == "__main__":
    # Example 1: 30 minutes (1800 seconds) starting now with initial HR of 75 BPM
    now = int(datetime.now().timestamp())
    
    print("=== Heart Rate Falling Asleep Simulation ===\n")
    
    # Generate sequence
    sequence = generate_heart_rate_sequence(
        start_time=now,
        duration_seconds=1800,  # 30 minutes
        initial_heart_rate=75,  # BPM
        interval_seconds=30      # Every 30 seconds
    )
    
    print(f"Generated {len(sequence)} data points")
    print(f"Duration: {sequence[-1]['timestamp'] - sequence[0]['timestamp']} seconds\n")
    
    # Show first few points as [timestamp, heart_rate] rows
    print("First 10 points:")
    for item in sequence:
        print(f"[{item['timestamp']}, {item['heart_rate']}],")
    
    # Statistics
    heart_rates = [item['heart_rate'] for item in sequence]
    print(f"\nStatistics:")
    print(f"  Initial HR: {heart_rates[0]} BPM")
    print(f"  Final HR: {heart_rates[-1]} BPM")
    print(f"  Average HR: {sum(heart_rates) / len(heart_rates):.1f} BPM")
    print(f"  Max HR: {max(heart_rates)} BPM")
    print(f"  Min HR: {min(heart_rates)} BPM")
    
    # Example 2: Save to JSON
    print("\n=== Saving to JSON ===")
    output_file = "heart_rate_sequence.json"
    with open(output_file, 'w') as f:
        json.dump(sequence, f, indent=2)
    print(f"Saved {len(sequence)} records to {output_file}")
    
    # Example 3: Different scenarios
    print("\n=== Other Scenarios ===")
    
    # Quick 10-minute sequence
    quick_seq = generate_heart_rate_sequence(
        start_time=now + 7200,
        duration_seconds=600,
        initial_heart_rate=80,
        interval_seconds=10
    )
    print(f"Quick scenario (10 min): {len(quick_seq)} points, HR {quick_seq[0]['heart_rate']} → {quick_seq[-1]['heart_rate']}")
    
    # Long 60-minute sequence
    long_seq = generate_heart_rate_sequence(
        start_time=now + 3600,
        duration_seconds=3600,
        initial_heart_rate=70,
        interval_seconds=5
    )
    print(f"Long scenario (60 min): {len(long_seq)} points, HR {long_seq[0]['heart_rate']} → {long_seq[-1]['heart_rate']}")
