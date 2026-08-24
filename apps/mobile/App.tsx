import { StatusBar } from 'expo-status-bar';
import { StyleSheet, Text, View } from 'react-native';

/**
 * Placeholder root.
 *
 * TASK-022 builds the capture hook only. The session screen that starts a
 * visit, drives `useAudioCapture`, and — critically — refuses to reach an
 * in-progress state while capture reports an error is TASK-025. Wiring a
 * half-built version of it here would be the exact failure that task exists to
 * prevent: a screen that looks like it is recording when it is not.
 */
export default function App() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>MedAuth AI</Text>
      <Text style={styles.body}>Session UI arrives in TASK-025.</Text>
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  title: {
    fontSize: 24,
    fontWeight: '600',
  },
  body: {
    marginTop: 8,
    textAlign: 'center',
  },
});
