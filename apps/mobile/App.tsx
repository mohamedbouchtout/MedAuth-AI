import { StatusBar } from 'expo-status-bar';
import { View, StyleSheet } from 'react-native';

import { VisitFlow } from './src/screens/VisitFlow';

/**
 * The SMART launch this app holds.
 *
 * `null` in every build, because **nothing here performs a SMART launch yet** —
 * that is TASK-025c, and the handoff it depends on is TASK-051e. Both routes
 * that identify a patient are keyed on a `launch_id`, because both spend the
 * launch's EHR access token, so without one this app can identify nobody, and
 * the picker says so rather than starting a visit against an invented
 * identifier.
 *
 * It is one named constant so that the remaining gap is a single value: when
 * TASK-025c lands, what replaces it is the launch that task obtains, and nothing
 * else on this path changes.
 */
const LAUNCH_ID: string | null = null;

/**
 * Root.
 *
 * Everything about the flow lives in `VisitFlow`, which is a component rather
 * than inline here so the whole path — launch context, search, selection, start
 * visit — can be driven in a test with a launch injected.
 */
export default function App() {
  return (
    <View style={styles.root}>
      <VisitFlow launchId={LAUNCH_ID} />
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
  },
});
